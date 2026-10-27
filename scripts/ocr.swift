import Foundation
import CoreGraphics
import Vision
import ImageIO

struct OCRLine: Codable { let text:String; let confidence:Float; let x:Double; let y:Double; let w:Double; let h:Double }
struct OCRPage: Codable { let pdf_page:Int; let text:String; let lines:[OCRLine]; let mean_confidence:Float?; let status:String; let error:String?; let elapsed_seconds:Double; let method:String; let raster_longest_edge_pixels:Int }
func render(_ path:String, _ n:Int, _ dpi:Double) throws -> CGImage {
 guard let doc=CGPDFDocument(URL(fileURLWithPath:path) as CFURL), let page=doc.page(at:n) else { throw NSError(domain:"pdf", code:1) }
 let box=page.getBoxRect(.mediaBox); let scale=dpi/max(box.width,box.height)
 let w=Int(ceil(box.width*scale)), h=Int(ceil(box.height*scale))
 guard let ctx=CGContext(data:nil,width:w,height:h,bitsPerComponent:8,bytesPerRow:0,space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGImageAlphaInfo.noneSkipLast.rawValue) else { throw NSError(domain:"render",code:1) }
 ctx.setFillColor(CGColor(gray:1,alpha:1));ctx.fill(CGRect(x:0,y:0,width:w,height:h));ctx.scaleBy(x:scale,y:scale);ctx.translateBy(x:-box.origin.x,y:-box.origin.y);ctx.drawPDFPage(page)
 guard let image=ctx.makeImage() else { throw NSError(domain:"render",code:2) }
 return image
}
func process(_ path:String,_ n:Int,_ dpi:Double) -> OCRPage {
 let start=Date()
 do {
  let img=try render(path,n,dpi)
  let req=VNRecognizeTextRequest();req.recognitionLevel = .accurate;req.recognitionLanguages=["zh-Hans","zh-Hant","en-US"];req.usesLanguageCorrection=true
  let handler=VNImageRequestHandler(cgImage:img,options:[:]);try handler.perform([req])
  let lines=(req.results ?? []).compactMap { o -> OCRLine? in
   guard let c=o.topCandidates(1).first else {return nil};let b=o.boundingBox
   return OCRLine(text:c.string,confidence:c.confidence,x:b.origin.x,y:b.origin.y,w:b.width,h:b.height)
  }
  let txt=lines.map{$0.text}.joined(separator:"\n")
  let conf=lines.isEmpty ? nil : lines.map{$0.confidence}.reduce(0,+)/Float(lines.count)
  return OCRPage(pdf_page:n,text:txt,lines:lines,mean_confidence:conf,status:lines.isEmpty ? "blank_or_unrecognized":"ok",error:nil,elapsed_seconds:Date().timeIntervalSince(start),method:"Apple Vision accurate zh-Hans zh-Hant en-US",raster_longest_edge_pixels:Int(dpi))
 } catch { return OCRPage(pdf_page:n,text:"",lines:[],mean_confidence:nil,status:"error",error:String(describing:error),elapsed_seconds:Date().timeIntervalSince(start),method:"Apple Vision",raster_longest_edge_pixels:Int(dpi)) }
}
let a=CommandLine.arguments
if a.count<5 { fputs("usage: ocr input.pdf outdir start end [workers=8] [longest-edge-pixels=2400]\n",stderr);exit(2) }
let input=a[1], out=a[2], start=Int(a[3])!, end=Int(a[4])!, workers=a.count>5 ? Int(a[5])! : 8, dpi=a.count>6 ? Double(a[6])! : 2400
try FileManager.default.createDirectory(atPath:out,withIntermediateDirectories:true)
let queue=OperationQueue();queue.maxConcurrentOperationCount=workers
let lock=NSLock(); var done=0
for n in start...end {
 let dest="\(out)/\(String(format:"%04d",n)).json"
 if FileManager.default.fileExists(atPath:dest) {continue}
 queue.addOperation {
  autoreleasepool {
   let result=process(input,n,dpi)
   do { let data=try JSONEncoder().encode(result);try data.write(to:URL(fileURLWithPath:dest),options:.atomic) } catch {fputs("write error page \(n): \(error)\n",stderr)}
   lock.lock();done+=1;fputs("page \(n) \(result.status) chars=\(result.text.count) sec=\(String(format:"%.2f",result.elapsed_seconds)) done=\(done)\n",stderr);lock.unlock()
  }
 }
}
queue.waitUntilAllOperationsAreFinished()

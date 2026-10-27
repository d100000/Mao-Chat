# 可选全文资料

公开仓库带有逐条核验的摘要、有限片段、日期资格、公开来源链接和方法解释。原始 PDF、完整 OCR 与生成索引保留为可选本地资产。

先运行 `python3 scripts/bootstrap_library.py` 即可创建解惑所需的资料库。它不会把资料卡说成已经导入了完整原文。全文不可用时，工作台仍可以使用已核摘要并说明资料缺口。

需要本地全文检索时，准备与所用版本对应的合法来源 PDF，使用 `scripts/run_ocr.py --source-dir /path/to/pdfs` 的配置方式生成本机 `corpus/manifest.json` 与页级 OCR，再执行：

```sh
python3 -m agent ingest
python3 -m agent index
python3 -m agent vector-index
```

完整导入仍校验底本指纹、页序、页数和 OCR 状态。更换版本需要重新核对证据定位，不能仅改文件名绕过校验。可选完整资料的集成测试会在资产齐备时运行；公开克隆中的确定性、认证、产品流程及已核资料测试照常执行。

# Legal Document Checks

Read-only, deterministic checks for contract DOCX review files and legal-service bid packages. Python standard library only.

为合同审查稿和法律服务投标材料提供机械校验：事项身份、原件指纹、批注关系、修订、占位符、项目名称、日期与报价算式。输入文件保持原样，结果可输出 JSON，命令行输出固定使用 UTF-8。

需要 Python 3.10 或更新版本，无第三方依赖。Windows 若没有 `python` 命令，可使用 `py -3`。

## 先试样例

### 合同事项绑定

```sh
python examples/contract_demo.py
```

样例在临时目录创建两个最小 OOXML 结构夹具，绑定标题、合同编号、双方、原件 SHA-256 和成果路径，完成检查后清理临时目录。它用于展示机械检查，不是可签署的合同范本。

正常结果应包含 `identity_gate: "pass"`、`summary.errors: 0`。不带身份记录的结构检查不能证明所选文件属于正确事项。

### 投标材料

```sh
python scripts/lint_bid_package.py --documents examples/bid/documents --requirements examples/bid/requirements.json --json
```

这是虚构项目的干净样例，退出码为 `0`。再试故意不一致的要求：

```sh
python scripts/lint_bid_package.py --documents examples/bid/documents --requirements examples/bid/requirements-mismatch.json --json
```

第二条命令应报告 `date_conflict` 和 `price_arithmetic`，并以 `1` 退出。

## 检查实际合同审查稿

仅检查结构：

```sh
python scripts/validate_contract_ooxml.py reviewed.docx --expected-author Reviewer --json
```

检查审查稿与原件的关系：

```sh
python scripts/validate_contract_ooxml.py reviewed.docx --baseline original.docx --identity identity.json --require-identity --expected-author Reviewer --mode review --json
```

`--expected-author` 应填写实际获准使用的修订和批注作者名。程序只检查作者标记，不增加标记、不伪造签名、不修改文档。

`identity.json` 使用以下字段，路径须为本机绝对路径：

```json
{
  "contract_number": "SYN-001",
  "title": "示例维护协议",
  "parties": ["示例委托单位", "示例服务单位"],
  "reviewing_party": "示例委托单位",
  "source_path": "/absolute/path/original.docx",
  "source_sha256": "填写原件的64位SHA256",
  "output_path": "/absolute/path/reviewed.docx"
}
```

上面的 JSON 是字段说明，必须替换为实际身份和原件指纹后使用；Windows 路径建议采用 `C:/folder/original.docx`，避免 JSON 反斜杠转义。

原件 SHA-256 可用 Python 计算：

```sh
python -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('original.docx').read_bytes()).hexdigest())"
```

无合同编号时使用 `null`，并确认原件确实没有已填写编号。成果必须另存，不能把原件本身或其硬链接当作成果。工具检查的是填写的身份与文件是否一致，不验证身份记录由谁批准或业务内容是否真实。

## 合同检查范围

- 校验当前可见标题、合同编号、当事人字段与原件对应位置；重复出现的原件标题或当事人声明逐处绑定。
- 原件哈希、原件与成果路径是否符合身份记录。
- 批注 ID 是否唯一、引用是否对应，以及是否正确链接到包内 `word/comments.xml`。
- 新增或变更的修订、批注作者是否符合指定值；保留原件已有历史修订。
- 未解决的占位符，以及近乎整段删除再插入的粗放修订。
- 正文、页眉页脚、脚注和尾注中的相关字段。

`review` 模式允许与原件对应的未变更下划线空白作为警告保留；新增空白和提示型占位符仍报错。准备签署前可以使用 `--mode finalization`，未解决空白按错误处理。

退出码 `0` 代表没有机械错误；警告仍可能存在。`1` 代表发现错误。参数用法错误返回 argparse 的 `2`。不带身份记录时，`identity_gate` 为 `not_checked`。

## 投标检查范围

`--documents` 指定目录，可读取 UTF-8 TXT、Markdown 和 DOCX 正文；文本和要求 JSON 均支持 UTF-8 BOM，无法按 UTF-8 解码的文本明确拒绝读取。`--requirements` 指定 JSON 要求，样例展示全部字段：

- `current_project_name` / `legacy_project_names`：本项目名称和需排查的旧项目名称。
- `mandatory_terms` / `scoring_items`：必备表述及评分项关键词覆盖。
- `dates`：标签附近出现的日期是否与指定日期一致；日历中不存在的日期和数字多写一位的日期报错。
- `price_checks`：JSON 中的单价 × 数量是否等于总价。

关键词出现不能证明实质响应，日期检查不证明遗漏期限已经被发现。报价算式检查的是提供的 JSON 数值，不会自动从报价表提取金额；数值应由使用者核对来源。DOCX 的删除修订等复杂情况仍需人工回读。

投标检查退出码为：`0` 无机械错误，`1` 存在检查错误，`2` 参数错误或输入无法读取。读取失败会返回 `input.invalid` 报告。空目录可用于单独检查 JSON 报价算式，`0` 不代表材料已经齐全；请同时查看 `summary.documents`。

工具不核验现行法律、证据真实性、采购公告原件、签章或实际递交回执，也不代替正式文件的内容及版面检查。适用于本机手动检查已知来源文件。

## 测试

```sh
python -B -m unittest discover -s tests -v
```

测试使用合成文本和内存生成的最小 OOXML 夹具，包含正常与异常身份、原件变更、批注断链、历史修订、占位符、项目混用、日期与报价异常，以及公开命令行样例。持续集成配置覆盖 Windows、macOS、Linux；实际通过的平台以 Actions 结果为准。

## 许可

MIT，见 [LICENSE](LICENSE)。

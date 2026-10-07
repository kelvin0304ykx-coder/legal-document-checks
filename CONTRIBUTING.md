# Contributing

提交问题时请提供 Python 版本、操作系统、运行命令、预期和实际行为。请用最小合成样例复现，避免上传真实客户文件、个人资料、凭据或生产日志。

修改后运行：

```sh
python -B -m unittest discover -s tests -v
```

行为变更应补充能复现问题的测试，并保持输入原件只读。报告格式或退出码变更请在 CHANGELOG 中说明。

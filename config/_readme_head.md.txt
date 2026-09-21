# 本地配置说明

本目录下的**运行时文件**默认已被 `.gitignore` 忽略，不会进入 Git 仓库。

仓库同样**不会上传**：`records/`（分析落盘）、`experience/`（经验库内容）、`logs/`、`trade_records/`（交易 CSV/截图）、`.env`、根目录临时图片与个人笔记等。仅源代码、`prompt_engineering/` 策略文本、`tests/` 与 `docs/` 说明文档会进入 GitHub。

## 首次使用

1. 复制模板为本地配置：

   ```cmd
   copy config\settings.example.json config\settings.json
   ```

2. 启动程序，在 **设置** 中填写你的 **API Key**（明文保存在本机 `config/settings.json`，该文件已被 git 忽略，不会入库）。

   也可直接编辑 `config/settings.json` 中的 `base_url`、`model` 等字段，Key 建议通过 GUI 保存以便字段格式校验。

3. `config/exception_state.json` 由程序在需要时自动创建，一般无需手动复制。结构可参考 `exception_state.example.json`。

4. 如需自定义 TradingView 品种别名，复制模板：

   ```cmd
   copy config\tv_symbol_aliases.example.json config\tv_symbol_aliases.json
   ```

> 本文档的字段表由 `scripts/gen_config_readme.py` 从 `pa_agent/config/settings.py` 与本机 `config/settings.json` 自动生成，
> 手工修改会在下次生成时被覆盖；要补充说明请改 `settings.py` 里字段上方的注释。

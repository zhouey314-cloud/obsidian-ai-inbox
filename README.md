# Obsidian AI Inbox

给已有 Obsidian 库加一个本机收件窗口；不是重建知识库，不是另一个云端笔记库。

## 你只需要做什么

This repository contains a local-only capture and keyword-search tool for an
existing Obsidian vault. It does not include a vault, private notes or a cloud
sync service.

安装并本机验证后，桌面有“收进第二大脑”图标。

- 存：打开入口，粘贴正文或选择文件，点“保存到我的 Obsidian”。
- 找：同一页面点“找以前的资料”，输入关键词，打开匹配到的实际笔记。
- 用：保存结果里点“复制给 Codex 的整理指令”，粘贴给本机 Codex。先针对这一份资料提炼，不再次全库整理。

## 每种材料怎么放

X 帖子、群聊：复制选中的相关正文，尽量附来源。程序不登录 X、不抓取微信、不读取整个剪贴板。只有链接时会明确标记“没有获取网页正文”。

会议文字、已有转写稿：粘贴或选择 TXT/MD/SRT/VTT 等文本文件，可以按正文搜索。保留来源，不自动生成已经核对的会议结论。

自己的想法：在文字框打字或使用电脑已有的系统语音输入。语音输入不是本程序新增的麦克风录音功能。

录音、图片、PDF、Word：保存原文件，同时建立一篇可找回附件的笔记，标记未转写/正文未提取。不会假装已经读取其中内容。需要时再交给本机 Codex 单独处理。

自己的 X 稿：类型选“我的X草稿”；本工具只保存稿件，不生成稿件、不替你发布。

## 数据具体保存在哪里

既有库：由安装命令通过 `--vault` 明确指定，安装时必须再次确认。

一般材料：既有 `00 Inbox/raw/`。

自己的 X 草稿：既有 `04 Content/X/`。

附件副本：`00 Inbox/raw/附件/`。这是唯一按需新增的库内文件夹；每份原件用内容哈希命名，避免同名覆盖。

程序本体放库外的用户 Application Support；桌面只有启动应用。不会修改旧正文、`.obsidian`、Skill 或 Git。

## 诚实的能力边界

这是“收集、落盘、关键词找回”的工具，不是会自主判断一切的第二大脑。

没有云 API、模型调用、自动摘要、音频转写、OCR、语义搜索、自动抓取微信/X、手机同步、后台批处理或自动发布。只处理主动交给它的材料；有无长期价值由你先选择，后续可针对一份让 Codex 提炼。

无需 API Key 或新增订阅。仅使用 Python 3.9+ 标准库；Mac 若没有可用 Python，安装任务必须报告，不擅自全局安装一套环境。

本程序没有向外网发送材料的代码，但既有 Obsidian/iCloud 等同步设置、你之后主动发给 Codex 的内容，属于其他程序的行为，不受此工具控制。

## 限制及异常情况

每次最多 12 个文件，单文件 25MB、合计 50MB；正文上限 2MB。大录音应交给本机 Codex 按路径处理，不要塞进此窗口。

文本附件提取上限：单文件 2MB，合计提取结果 2MB；原文件仍完整保存。优先 UTF-8，兼容能正确解码的 UTF-16、GB18030；不能可靠解码就标记未提取，不编造内容。

关键词检索只读取库里的可读 Markdown，不遍历隐藏配置、软链接、node_modules 或附件目录。超过 8MB 或不能解码的笔记会跳过并在搜索结果里报告数量；这不是“已经丢失”。显示最相关的前 25 项。

“最近收集”只显示本入口保存的最近 20 份，不代表整个旧库只有这些内容。

保存采用独占创建、磁盘刷新和读回核对。相同来源/类型/原文/附件会返回完整的已有收集笔记；旧笔记被编辑或上次写入不完整时，不覆盖旧笔记而创建新副本。附件和笔记多文件保存不是一个文件系统事务：断电或中途失败可能留下已保存附件/不完整的新笔记，但不会自动删除或回滚任何旧文件。失败提示时保留输入，让 Codex 核查后重试。

## 启动、退出、卸载

运行 `./install.command` 或直接执行 `python3 install_mac.py --vault /path/to/existing-vault`，会在本机创建桌面入口。启动后在本机浏览器打开一个仅绑定 127.0.0.1 随机端口的窗口。

每次启动生成新令牌；接口检查 Host、请求来源和令牌。不暴露给局域网，不创建登录项，不建立定时任务，不监控系统。

页面“关闭入口”会停止当前进程；没有 API 交互 30 分钟后进程也会退出。退出前应保存输入。打开多个入口会启动多个进程，但同库保存有进程锁避免同一份并发重复写入。

要卸载，让 Codex 仅删除安装记录里的桌面入口和该版本库外程序目录。不要删除任何 Obsidian 笔记和附件，不运行全盘清理。

## 已验证 / 未验证

构建环境：Python 3，临时模拟库；测试夹具不是用户真实资料。

JavaScript 语法检查通过。浏览器交互测试被当前运行环境对本机网址的策略阻止（ERR_BLOCKED_BY_ADMINISTRATOR），未完成；没有绕过或修改该策略。详见 BROWSER_TEST_RESULTS.json，须由本机 Codex 补做。

没有访问用户的 Mac；桌面 AppleScript 应用构建、系统授权、浏览器唤起和 Obsidian 跳转尚需本机验证。不得把这些写成已经完成。

## Demo Vault

[`demo-vault/`](demo-vault/) is a tiny synthetic fixture for local checks. It
is not a copy of a personal vault. Use a real vault only when you explicitly
pass its path to the installer.

## Official references

Obsidian 使用普通本地 Markdown 文件，外部文件变更会刷新到库：
https://obsidian.md/help/data-storage

Obsidian 官方 URI 支持通过编码后的绝对路径打开笔记：
https://obsidian.md/help/Extending%2BObsidian/Obsidian%2BURI

以上仅为实现依据，不代表 Obsidian 官方开发、审核或认可了这个第三方小工具。

## License

MIT. See [LICENSE](LICENSE).

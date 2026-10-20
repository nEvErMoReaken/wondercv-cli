---
name: wondercv
license: MIT
description: 使用超级简历 WonderCV 的命令行客户端扫码登录、读取、修改和导出简历。当用户提到超级简历/wondercv、要扫码授权或让 AI 改该网站里的简历、调整模块/小标题/主题、上传头像/校徽/作品图片、导出 PDF/PNG，或恢复失败的简历操作时使用。通用求职建议、其他网站登录或没有使用 WonderCV 的文档写作不触发。
compatibility: Requires Python 3, network access, and the user's WonderCV account. Includes the CLI. Uses official mini-program images, native terminal image protocols where available, and local file links otherwise.
---

# WonderCV

通过随 Skill 安装的脚本操作用户自己的超级简历账号。官网：https://www.wondercv.com/ 。这是非官方客户端；账号权限和会员权益由官网决定。

## 1. 检查环境与授权

从本 Skill 的实际加载位置确定旁边 `scripts/wondercv.py` 的绝对路径，设为 `WONDERCV_SCRIPT`。不要假设用户当前目录是代码仓库，也不需要另外克隆仓库。

```bash
python3 "$WONDERCV_SCRIPT" --help
```

先只读检查授权：

```bash
python3 "$WONDERCV_SCRIPT" --json auth status
```

未登录时，优先使用官方微信扫码流程，不让用户手动寻找 Cookie：

1. 先确认用户准备扫码，以及宿主终端是否支持图片展示；不能确认时采用文件链接。运行以下命令取得约 60 秒有效的官方小程序码原图 `qr_path` 和本地文件链接 `qr_uri`。若已有凭证文件，先告知用户并确认替换，之后才加 `--replace`；失败不会覆盖旧凭证。
   ```bash
   python3 "$WONDERCV_SCRIPT" --json auth login --start --image-only
   ```
2. 宿主支持图片时立即展示 `qr_path` 原图；不支持或能力未知时，把完整的 `qr_uri` 放在用户可见的回复里。文件链接属于脚本所在电脑，SSH 运行时先把图片安全下载到用户所在电脑，再给对应的本机文件链接。工具读到图片不代表用户已经看见；不以浏览器控制台、隐藏工具输出或字符画替代。
3. 展示后调用等待钩子，而不是等用户再打字说“扫好了”：
   ```bash
   python3 "$WONDERCV_SCRIPT" --json auth wait --timeout 60
   ```
4. `status=waiting` 仍未登录，不能当成功；过期时重新生成并展示新码，不无限轮询旧码。取消用 `auth cancel`，它不删除已有凭证或官网设备。
5. 只有 `status=authenticated` 才表示设备登记、只读验证及权限为 600 的凭证保存均完成。若 `environment_override=true`，告知用户旧 `WONDERCV_TOKEN` 仍优先，需要取消或更新环境变量，再运行 `auth status` 核对。

用户直接运行终端命令时，使用 `auth login`：CLI 根据终端环境标识和 TTY 检测，在支持的 iTerm2 / WezTerm / Kitty 协议上显示图片，否则输出 `file://` 链接并等待。tmux / screen、重定向及未知终端回退到链接。Kitty 显示官方 JPEG 需要环境已有 Pillow；缺少时同样回退，不自动安装。使用官网小程序码原图，不走公众号二维码、不重新编码成普通码、不生成字符画。

扫码会登记名为 `WonderCV CLI` 的常用设备。设备名额满、需要手机绑定或新账号完善资料时，提示用户去官网处理；不自动踢掉其他设备，不绕过官网要求。二维码及 `login/session.json` 是短期敏感登录材料，不上传到图床、Issue、Git 或公开日志。

仍支持已有凭证：环境变量 `WONDERCV_TOKEN` 优先于 `~/.config/wondercv/token`。用户选择手动方式时，在官网登录后把 `_t` 安全存入本机权限为 600 的文件、目录权限 700；不让用户把凭证、密码、验证码贴进对话，也不打印凭证文件。

完成条件：脚本可运行，`auth status` 只读验证成功；否则报告具体阻塞，不继续编辑简历。

## 2. 确认目标与修改内容

- 用户尚未指定简历时，展示简历名供选择，不凭列表第一项猜目标。
- 通过 `--json resume dump <token>` 读取目标完整内容。简历 token 是对象标识，与登录凭证不同。
- 先给出拟修改内容，保留用户的真实经历；对缺失事实提问，不编造任职、项目或数字。
- 用户未明确授权直接修改原件时，先提议 `resume copy <token> --name <副本名>`，取得同意后操作副本。复制不代表所有对象都已隔离，保留 CLI 的共享对象保护。
- 分享公开、删除、永久删除、批量操作、覆盖已有文件分别取得明确授权；“帮我改简历”不包含这些授权。

完成条件：用户确认目标和改动范围，才能进入写入。

## 3. 调用 CLI，逐步保存并核对

全局 `--json` 放在命令组之前；通过对应子命令的 `--help` 确认参数。复用 CLI 的校验和引用保护，不绕过客户端直接猜测接口写入。

常见入口：

```bash
# 完整读取与复制
python3 "$WONDERCV_SCRIPT" --json resume dump "$CV_TOKEN"
python3 "$WONDERCV_SCRIPT" resume copy "$CV_TOKEN" --name '投递版'

# 只更新传入字段
python3 "$WONDERCV_SCRIPT" profile set "$CV_TOKEN" username=张三
python3 "$WONDERCV_SCRIPT" work set "$CV_TOKEN" "$WORK_ID" --title '后端工程师'
python3 "$WONDERCV_SCRIPT" extra set "$CV_TOKEN" skill --desc 'Python、Go、MySQL'

# 模块总标题与技能块小标题是不同目标
python3 "$WONDERCV_SCRIPT" module title "$CV_TOKEN" edus --title '教育背景'
python3 "$WONDERCV_SCRIPT" module title "$CV_TOKEN" skill --title '专业技能'

# 主题与版式
python3 "$WONDERCV_SCRIPT" format options
python3 "$WONDERCV_SCRIPT" format theme "$CV_TOKEN" --color '#4183FF'

# 图片与导出
python3 "$WONDERCV_SCRIPT" profile avatar set "$CV_TOKEN" --file ./avatar.png
python3 "$WONDERCV_SCRIPT" resume export "$CV_TOKEN" --format pdf --out ./resume.pdf
```

- `CV_TOKEN`、各对象 ID 来自只读查询或创建结果，不能使用示例数字猜测。
- 经历用 `edu/work/org`，自定义模块及条目用 `section/item`，作品用 `opus`，校徽用 `school-logo`；五类技能块为 `skill/language/certificate/interest/other`。
- 重排必须先读取当前完整 ID 集合，再提交其完整排列，不凭片段列表替换。
- 每次写入后重新读取，核对目标字段及未修改内容；“接口成功”不等于内容和排版已验收。

完成条件：授权范围内的修改均保存且重读核对，失败则按下一节处理。

## 4. 处理失败与敏感操作

- 创建结果未知时停止自动重试。运行 `recover` 列出未决操作，再运行 `recover <操作编号>` 只读查看候选。向用户解释候选和影响，取得选择后才使用 `--attach/--discard/--adopt/--none`。
- 部分成功要逐项说明哪些已写入、未执行、结果未知；不能把整条命令说成“完全没改”。
- 回收站不可读或共享引用无法排除时，接受保守拒绝。不要清空回收站、恢复其他简历或添加 force 来绕过保护。
- 图片仅支持 PNG/JPEG、2 MB 以内。上传失败不能宣称头像/附件已经更换；修改作品标题时保留原二维码，移除附件需单独授权。
- 平台频控、会员权限、登录失效分别报告，不自动购买权益或尝试绕过。
- TLS 错误保持证书校验；可使用用户信任的 `WONDERCV_CA_FILE`，或由用户选择可信运行环境。不要使用 `-k`、关闭校验或自动信任连接中获取的证书。
- 默认不覆盖导出文件。分享设置读回不代表访客访问、隐私和撤销效果已验证。

完成条件：已解决的错误有重读证据；未解决的状态、对象及下一步清楚，不留下无法解释的自动重试。

## 5. 交付

给出修改摘要、实际保存结果和导出文件路径。需要排版验收时打开真实 PDF/PNG 检查内容、顺序和图片，不以空白截图或仅检查文件存在代替。明确列出仍需用户授权或外部条件的事项。

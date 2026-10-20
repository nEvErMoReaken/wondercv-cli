# wondercv-cli

让 AI 通过命令行帮你维护[超级简历 WonderCV](https://www.wondercv.com/)里的简历。

提供 Python 命令行客户端和一个自带脚本的 Agent Skill。支持内容编辑、模块排序、标题与主题调整、图片上传，以及 PDF/PNG 导出。Python 脚本仅使用标准库，无需安装 pip 依赖。

## 为什么做这个

从上学到现在工作，超级简历一直是我非常喜欢用的简历制作工具。为了支持它，我还充了永久会员。

但手动编写、反复修改简历实在太麻烦，所以我直接让 **Claude Opus 5.5** 写了一个 CLI 版本，让 AI 能通过命令行帮我维护简历、调整排版，再导出文件。

这个项目是非官方客户端，仍然使用超级简历提供的服务。如果你也喜欢这个工具，欢迎去[官网](https://www.wondercv.com/)体验和支持。需要会员的功能，也请支持官方会员。

## 安装 Skill

需要 Python 3。以下安装命令使用 [Skills CLI](https://github.com/vercel-labs/skills)，当前要求 Node.js 22.20 或更高版本。

安装到当前项目的 Claude Code：

```bash
npx skills add nEvErMoReaken/wondercv-cli --skill wondercv -a claude-code -y
```

让安装器选择其他受支持的助手：

```bash
npx skills add nEvErMoReaken/wondercv-cli --skill wondercv
```

如需跨项目使用，加上 `-g`。Skill 目录内已携带 Python 脚本，不需要再克隆仓库。安装 Skill 不会自动获得账号权限，首次使用仍需本人登录授权。

安装后可以对助手说：

> 先读取我的超级简历，给我修改建议，等我确认后再复制一份修改。保留真实经历，突出后端开发能力，最后导出 PDF 给我检查。

Skill 会先确认目标和改动范围，再执行保存、重新读取和导出。它不内置 AI 模型，也不会替你购买会员。

技能格式与流程见 [`skills/wondercv/SKILL.md`](skills/wondercv/SKILL.md)。技能的目录展示由 [skills.sh](https://skills.sh/) 根据安装数据处理，不能保证安装后立即出现在榜单上。

## 直接运行脚本

不使用 Skill 也可以：

```bash
git clone https://github.com/nEvErMoReaken/wondercv-cli.git
cd wondercv-cli
python3 wondercv.py --help
```

目前以 Python 脚本提供，不会安装系统级 `wondercv` 命令。根目录脚本是兼容入口，实际实现只有一份，位于 `skills/wondercv/scripts/wondercv.py`。

### 配置凭证

先在[官网](https://www.wondercv.com/)登录，取得 Cookie 中 `_t` 的值。凭证支持：

- 环境变量 `WONDERCV_TOKEN`；或
- 文件 `~/.config/wondercv/token`，文件权限为 `600`，目录权限为 `700`。

例如，在 Bash 中隐藏输入，避免把凭证直接写进命令历史：

```bash
read -rsp 'WonderCV token: ' WONDERCV_TOKEN
printf '\n'
export WONDERCV_TOKEN
```

然后查询：

```bash
python3 wondercv.py resume list
```

登录凭证具有账号访问权限，不要发送到公开对话、Issue 或日志中，也不要提交到 Git。凭证失效后需要重新登录并更新；CLI 不处理验证码。

## 常用操作

下面的 `CV_TOKEN` 是查询结果里的简历标识，不是登录凭证。将示例值替换为你确认要操作的简历。

```bash
CV_TOKEN='替换为目标简历 token'

# 可读摘要与完整 JSON
python3 wondercv.py resume show "$CV_TOKEN"
python3 wondercv.py --json resume dump "$CV_TOKEN"

# 创建副本，拿到新 token 后再编辑副本
python3 wondercv.py resume copy "$CV_TOKEN" --name '后端工程师投递版'

# 只更新传入的个人信息字段
python3 wondercv.py profile set "$CV_TOKEN" username=张三 email=example@example.com

# 添加经历与设置技能
python3 wondercv.py work add "$CV_TOKEN" --company '示例公司' \
  --title '后端工程师' --start 2023-07 --present \
  --description '<ul><li>负责核心服务开发与维护</li></ul>'
python3 wondercv.py extra set "$CV_TOKEN" skill --desc 'Python、Go、MySQL'

# 模块总标题与技能小标题
python3 wondercv.py module title "$CV_TOKEN" edus --title '教育背景'
python3 wondercv.py module title "$CV_TOKEN" skill --title '专业技能'

# 查询排版选项、设置整体主题色与基础版式
python3 wondercv.py format options
python3 wondercv.py format theme "$CV_TOKEN" --color '#4183FF'
python3 wondercv.py format set "$CV_TOKEN" font=font-2 font_size=14 page_margin=10

# 上传头像或自定义校徽（PNG/JPEG，不超过 2 MB）
python3 wondercv.py profile avatar set "$CV_TOKEN" --file ./avatar.png
python3 wondercv.py school-logo upload "$CV_TOKEN" --file ./logo.png

# 导出；默认不覆盖已有文件，明确需要覆盖时才加 --force
python3 wondercv.py resume export "$CV_TOKEN" --format pdf --out ./resume.pdf
python3 wondercv.py resume export "$CV_TOKEN" --format png --out ./resume.png
```

其他命令包括 `edu`、`org`、`section`、`item`、`opus`、`share`、`template` 和各类排序。通过对应命令的 `--help` 查看参数。全局 `--json` 必须放在命令组之前。

## 安全与恢复

- 先读取和确认再写入；优先在副本上编辑，内容事实仍由本人确认。
- 网站复制的部分自定义模块仍与原件共享。CLI 会阻止已识别的不安全修改，复制并不保证所有对象天然隔离。
- 回收站里的引用无法完整读取时，涉及共享对象的部分改名、删除及孤立对象清理会保守拒绝，没有强制绕过选项。
- 创建请求结果未知时不自动重发，以免生成重复内容。未决操作保存在受限权限的本地状态目录，可通过 `python3 wondercv.py recover` 查看候选，再显式决定如何处理。
- 修改作品标题、链接时保留原二维码；清除附件需要明确传入移除参数。
- 导出先写临时文件再原子发布；未加 `--force` 不会覆盖已有文件。
- HTTPS 证书校验保持开启，认证 API 不自动跟随重定向。企业网络可使用可信的 `WONDERCV_CA_FILE`，不要关闭校验。
- 未决操作目录默认在 `~/.config/wondercv/state/`，可通过 `WONDERCV_STATE_DIR` 修改；不要上传该目录、凭证或导出的私人简历。

退出码：`0` 成功，`1` 接口业务或内部错误，`2` 命令用法错误，`3` 输入/配置或安全拒绝，`4` 网络/服务端故障。错误写入 stderr，并区分部分完成与结果未知。

## 验证与限制

已在 Python 3.12 环境运行自动化测试，并在真实账号的测试简历上验证头像、自定义校徽、作品图片上传，PDF/PNG 下载，及代表性主题和导出效果。导出文件已实际打开检查，不只是检查接口返回成功。

运行不访问真实账号的测试：

```bash
python3 -m unittest \
  tests.test_unit tests.test_rework tests.test_reacceptance \
  tests.test_upload tests.test_headmodule tests.test_distribution -v
```

真实账号测试是显式开启的：`WONDERCV_LIVE=1 python3 -m unittest tests.test_live -v`。它会创建或复用测试夹具并执行写入，**不是只读检查**。运行前确认账号、夹具及清理安排；频控或外部条件造成的跳过不计为通过。

已知边界：

- 使用网站接口，兼容性可能随官网更新变化。
- 会员权益、回收站容量和删除频控由平台决定，CLI 不绕过限制。
- 分享设置可读写，但第二访客的访问控制与撤销效果仍待完整验证。
- 仅核对过代表性主题的视觉效果，不代表所有排版组合均已逐一检查。

## 许可与声明

项目代码采用 [MIT License](LICENSE)。自包含 Skill 也携带许可证。

本项目是非官方客户端，与 WonderCV 官方无隶属关系。超级简历的名称、网站和服务属于其各自权利人；本项目的 MIT 许可不授予对官方服务的额外权限。请仅操作本人或已获授权的账号和简历，遵守平台条款。

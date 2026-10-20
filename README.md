# wondercv-cli

[超级简历 WonderCV](https://www.wondercv.com/) 的非官方命令行客户端，附带一个自包含的 Agent Skill。

可以让 AI 助手直接操作网站里的简历，也可以自己运行脚本。支持内容编辑、模块排序、标题与主题调整、图片上传和 PDF/PNG 导出，登录使用微信小程序扫码。

## 为什么做这个

从上学到现在工作，超级简历一直是我很喜欢用的简历工具。为了支持它，我还充了永久会员。

但手动编写、反复修改简历实在太麻烦，所以我直接让 Claude 写了这个 CLI。现在可以把修改交给 AI，继续用超级简历保存和导出。

如果你也喜欢超级简历，欢迎去[官网](https://www.wondercv.com/)支持官方会员。这个项目只是换了一种操作方式，仍然使用它的服务，会员功能也照常需要会员。

## 安装 Skill

脚本需要 Python 3；安装 Skill 使用 [Skills CLI](https://github.com/vercel-labs/skills)，当前要求 Node.js 22.20+。

安装到当前项目的 Claude Code：

```bash
npx skills add nEvErMoReaken/wondercv-cli --skill wondercv -a claude-code -y
```

使用其他助手时，交给安装器选择：

```bash
npx skills add nEvErMoReaken/wondercv-cli --skill wondercv
```

全局安装加 `-g`。Skill 自带脚本，不必另行克隆仓库；首次使用时由助手发起扫码登录，在手机上确认授权即可。

比如：

> 先读取我的超级简历，给我修改建议，等我确认后再复制一份修改。保留真实经历，突出后端开发能力，最后导出 PDF 给我检查。

Skill 约定了先确认改动、保存后重读核对的流程，详见 [`SKILL.md`](skills/wondercv/SKILL.md)。模型由你使用的助手提供，CLI 本身不接模型。

[在 skills.sh 查看](https://skills.sh/nevermoreaken/wondercv-cli/wondercv)。

## 直接运行脚本

只用 CLI 的话：

```bash
git clone https://github.com/nEvErMoReaken/wondercv-cli.git
cd wondercv-cli
python3 wondercv.py --help
```

核心操作只用 Python 标准库，无需 pip 安装，也不提供系统级 `wondercv` 命令。根目录脚本是兼容入口，实现位于 `skills/wondercv/scripts/wondercv.py`。

### 微信扫码登录

运行后用微信扫码，在手机上确认：

```bash
python3 wondercv.py auth login
```

使用的是官网原始小程序码，不走公众号二维码，也不重新编码。CLI 根据终端环境标识和 stderr 的 TTY 状态选择展示方式：

- iTerm2 / WezTerm 直接显示图片；Kitty / Ghostty / Warp 使用 Kitty 图片协议。官方返回 JPEG 时，后者需要环境已有 Pillow 才能转换显示，磁盘上的原图不变；缺少时回退到链接，不自动安装。
- 未知终端、tmux / screen 或输出被重定向时，给出 `file:///…` 文件链接。没有字符画、临时网页或图床。
- SSH 场景的文件链接指向远端，需先下载到本机再打开。

码约 60 秒有效。确认后会按官网要求登记 `WonderCV CLI` 常用设备，验证凭证，再原子保存到 `~/.config/wondercv/token`（文件 600、目录 700）。过期、取消、设备登记或验证失败都会保留旧凭证；替换已有凭证需加 `--replace`。

助手可以分两步拿图、等待：

```bash
# 返回 qr_path 和 qr_uri，先展示原图或文件链接
python3 wondercv.py --json auth login --start --image-only

# 展示后立即等待扫码结果
python3 wondercv.py --json auth wait --timeout 60

python3 wondercv.py auth status
python3 wondercv.py auth cancel
```

`waiting` 表示仍在等待，`authenticated` 才表示设备登记、校验和保存均已完成。过期后重新生成；`auth cancel` 只清理待扫码材料，不注销已有登录。设备名额满、需要绑定手机或完善账号时，需到官网处理，CLI 不会删除其他设备或绕过验证码。

### 使用已有凭证

也可以使用官网登录 Cookie `_t`：设置 `WONDERCV_TOKEN`，或保存到 `~/.config/wondercv/token`。环境变量优先；扫码结果若含 `environment_override=true`，记得取消或更新旧变量，否则后续仍使用旧凭证。

凭证、二维码和待扫码会话都按敏感信息处理，不放进 Git、公开日志、Issue 或图床。凭证失效后重新扫码即可。

## 常用操作

先用 `resume list` 找到目标简历。下文的 `CV_TOKEN` 是简历标识，与登录凭证不同。

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

教育经历、组织经历、自定义模块、作品、分享和模板分别见 `edu`、`org`、`section/item`、`opus`、`share`、`template`。各子命令支持 `--help`；需要结构化输出时，将全局 `--json` 放在命令组前。

## 安全与恢复

建议先读完整内容，在副本上修改，最后核对导出文件。涉及经历和数字，仍需本人确认。另有几处需要留意：

- 网站复制的部分自定义模块会与原件共享，副本并非完全隔离。CLI 会拦截已识别的不安全修改；回收站引用无法完整读取时，相关改名、删除和孤立对象清理也会拒绝，没有 force 绕过。
- 创建请求结果未知时不自动重发。用 `python3 wondercv.py recover` 查看未决操作和候选，再决定如何处理，避免重复创建。
- 修改作品标题、链接会保留原二维码；移除附件需明确传参。导出先写临时文件再原子发布，默认不覆盖，覆盖需加 `--force`。
- HTTPS 证书校验始终开启，认证 API 不跟随重定向。企业网络可通过 `WONDERCV_CA_FILE` 加载可信 CA。

未决操作保存在 `~/.config/wondercv/state/`，可用 `WONDERCV_STATE_DIR` 改位置。这个目录和导出的私人简历都不应公开。

退出码：`0` 成功，`1` 接口业务或内部错误，`2` 命令用法错误，`3` 输入/配置或安全拒绝，`4` 网络/服务端故障。错误写入 stderr，并区分部分完成与结果未知。

## 验证与限制

当前离线回归共 180 项，已在 Python 3.8 和 3.12 下通过。真实账号验证只操作测试简历，覆盖头像、自定义校徽、作品图片上传、PDF/PNG 下载及代表性主题；导出文件也已打开检查。

离线测试：

```bash
python3 -m unittest \
  tests.test_unit tests.test_rework tests.test_reacceptance \
  tests.test_upload tests.test_headmodule tests.test_distribution tests.test_login -v
```

登录与终端图片测试使用合成响应，覆盖原图字节、能力检测、链接回退、JSON 输出隔离和凭证保护。

真实账号测试需显式开启：

```bash
WONDERCV_LIVE=1 python3 -m unittest tests.test_live -v
```

它会创建或复用测试夹具并写入账号数据，运行前需确认账号和清理安排。因频控或外部条件跳过的项目不算通过。

仍未验证完整的部分：

- 小程序码生成已实测，扫码确认到凭证保存的完整链路还没有完成真实验收。
- 分享设置可读写，但第二访客的访问控制、撤销效果仍待验证。
- 只检查过代表性主题，未逐一检查所有排版组合。

接口兼容性可能随官网更新变化；会员权益、回收站容量和删除频控均以平台为准。

## 许可与声明

代码采用 [MIT License](LICENSE)，Skill 内附同一份许可证。

本项目与 WonderCV 官方无隶属关系。超级简历的名称、网站和服务属于其各自权利人，MIT 许可不包含官方服务的额外权限。仅用于本人或已获授权的账号和简历，遵守平台条款。

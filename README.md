# bhelp

`bhelp` 是一个面向 Linux/WSL 的双语命令行帮助阅读器。它捕获 `man` 页面、命令的 `--help` 输出或标准输入中的文本，自动识别原语言，再把译文紧跟在对应原文块的下方。

```text
DESCRIPTION

    Clones a repository into a newly created directory.
    将仓库克隆到一个新创建的目录中。
```

翻译以段落、选项说明和列表项等“语义块”为单位，而不是机械逐行翻译。命令、选项、路径、URL、环境变量和代码片段会尽量保持原样。DeepSeek 和 OpenAI 使用同一套 provider 接口，可以在配置或单次命令中切换。

## 安装

当前版本面向装有 Python 3.11+ 的 Linux/WSL，运行时不依赖任何第三方 Python 包。首选使用仓库中的零第三方依赖安装脚本：

```bash
git clone <repository-url> bhelp
cd bhelp
sh scripts/install.sh
```

脚本会构建单文件 zipapp `dist/bhelp`，并安装为 `~/.local/bin/bhelp`。如果该目录还不在 `PATH` 中，请按脚本提示将它加入 shell 配置，然后重新打开终端。

只有需要修改源码或以 Python 包方式开发时，才需要可选的 venv/pip 流程：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

确认命令已经可用：

```bash
bhelp --help
```

如果发行版缺少完整手册，请安装对应的 `man-db` 和命令手册包。`git clone -h` 只是简短帮助，`git help clone`/`bhelp man git-clone` 依赖完整的 Git man page。

## 三种输入方式

### 1. `man` 页面

```bash
bhelp man git-clone
bhelp man --to ja-JP git-clone
```

`bhelp` 会主动禁用内层 pager 并请求纯文本，避免 `man`、`less` 和 `bhelp` 的分页器相互嵌套。

### 2. 运行命令并捕获帮助

```bash
bhelp run -- git clone -h
bhelp run --from auto --to zh-CN -- curl --help
```

`--` 后面的内容按参数数组直接执行，不经过 shell 拼接。stdout 和 stderr 都会被捕获，因为不少程序把帮助写到 stderr。

不要把需要交互输入、会修改系统状态或会无限运行的命令交给 `bhelp run`；这个入口的目标是读取帮助文本。

### 3. 从管道读取

```bash
command --help 2>&1 | bhelp pipe
man git-clone | col -bx | bhelp pipe
```

管道右侧的退出状态默认是 `bhelp` 的状态。如果脚本还需要感知上游命令失败，请先启用：

```bash
set -o pipefail
command --help 2>&1 | bhelp pipe
```

标准输出连接终端时，长内容默认交给 `less`；重定向到文件或另一个管道时会自动关闭颜色和分页。也可以设置 `NO_COLOR=1` 禁用颜色。

## Git 原生命令集成

Git 支持自定义 man viewer，因此无需创建一个会遮蔽系统 `git` 的 wrapper。安装集成后仍然输入原命令：

```bash
bhelp integrate git install
git help clone
```

检查当前状态：

```bash
bhelp integrate git status
```

安装器写入的 Git 配置通常类似于：

```bash
git config --global man.viewer bhelp
git config --global man.bhelp.cmd '/home/user/.local/bin/bhelp man'
git config --global help.format man
```

`man.bhelp.cmd` 会使用安装时解析到的 bhelp 绝对路径，而不是固定写成上面的示例路径；如果安装命令传入 `--config PATH`，它还会携带该配置文件的绝对路径。安装器会先把现有 `man.viewer`、`man.bhelp.cmd` 和 `help.format` 备份到 bhelp 用户配置，再把 `help.format` 临时设为 `man` 并启用 bhelp viewer。移除集成时会恢复这三项安装前的值：

```bash
bhelp integrate git remove
```

## 语言与 provider

源语言默认是 `auto`，由翻译模型根据每个语义块判断。目标语言默认是 `zh-CN`。BCP 47 风格的语言标签可以用于其他语言，例如：

```bash
bhelp man --from auto --to zh-TW printf
bhelp run --to ja-JP -- rg --help
bhelp pipe --provider openai --model gpt-5.6-luna < help.txt
```

当显式指定的源语言与目标语言相同时，`bhelp` 不请求 API，也不会重复输出同一段文字。单次命令参数优先于配置文件。

所有读取文本的子命令都支持这些公共选项：

```text
--from/-f LANGUAGE        源语言，默认为 auto
--to/-t LANGUAGE          目标语言
--provider/-p NAME        deepseek 或 openai
--model MODEL             覆盖 provider 的默认模型
--base-url URL            覆盖 API 地址
--config PATH             使用另一份配置文件
--no-cache                本次不读取或写入翻译缓存
--refresh                 忽略旧值并刷新缓存
--no-network              只显示原文，不读缓存也不访问 API
--pager auto|always|never 分页策略
--color auto|always|never 颜色策略
--strict                  把翻译失败视为退出码 3
--quiet                   不显示非必要诊断信息
```

## 配置 DeepSeek 或 OpenAI

用户配置默认位于：

```text
~/.config/bhelp/config.json
```

先初始化一份带安全权限的用户配置：

```bash
bhelp config init
bhelp config path
```

仓库中的 [`config.example.json`](config.example.json) 展示完整结构，但不含任何真实密钥。如果需要手动复制它，请同时收紧目录和文件权限：

```bash
install -d -m 700 ~/.config/bhelp
install -m 600 config.example.json ~/.config/bhelp/config.json
```

通过无回显提示把密钥保存到该配置文件：

```bash
bhelp config set-key deepseek
# 或
bhelp config set-key openai
```

`set-key` 不把密钥放进命令参数、shell history 或进程列表；配置文件保持 `0600`。也可以手动编辑 provider 的 `api_key`，但必须重新确认权限：

```bash
chmod 700 ~/.config/bhelp
chmod 600 ~/.config/bhelp/config.json
```

查看当前配置时，密钥会被脱敏：

```bash
bhelp config show
```

默认 DeepSeek 模型是 `deepseek-v4-flash`，默认 OpenAI 模型是 `gpt-5.6-luna`。`model` 必须是账户实际可用的模型；默认值和示例值不是账户权限保证。切换默认 provider 只需修改顶层 `provider`，也可用 `--provider` 和 `--model` 仅覆盖本次运行。

测试配置、网络和短翻译请求：

```bash
bhelp doctor --provider deepseek
```

自定义 `base_url` 应使用可信 HTTPS 地址，避免把 Authorization 请求头发送给未知服务器。

## 密钥与内容安全

- `bhelp` 会把待翻译的帮助文本发送给所选云端 provider。不要用它处理包含口令、令牌、私有源码或其他敏感数据的输出。
- API 密钥不会成为缓存键，也不应写入缓存、普通日志或错误消息；诊断信息必须脱敏。
- `config show`、`doctor`、普通错误和诊断输出都必须隐藏完整密钥。
- 原文和模型返回文本中的危险终端控制序列会被清理，防止 OSC 52、终端标题和双向覆盖字符造成注入或视觉欺骗。
- 如果密钥曾经出现在聊天记录、公开仓库、截图或共享日志中，请立即在 provider 控制台撤销并生成新密钥。修改本地文件权限不能让已经泄露的密钥重新变安全。

## 缓存

翻译缓存默认位于：

```text
~/.cache/bhelp/v2/*.json
```

每个缓存条目都是一个以 SHA-256 缓存键命名的 JSON 文件。缓存键包含规范化原文、源语言、目标语言、provider、`base_url`、model 和提示词/数据结构版本。终端宽度、颜色和 API 密钥不会进入缓存键，因此同一译文可以在不同终端宽度下重新排版而无需重复付费请求。

`--no-network` 是完全离线的原文模式：它既不访问 API，也不读取已有翻译缓存。

临时绕过缓存：

```bash
bhelp man --no-cache git-clone
```

查看统计信息或清空缓存：

```bash
bhelp cache stats
bhelp cache clear
```

缓存和配置相互独立；清缓存不会删除密钥或 provider 设置。

## 失败与退出行为

翻译是原始帮助之上的增强层：网络超时、配额不足、401/403、429、5xx 或模型响应无效时，`bhelp` 仍会输出能取得的原文，并把简短且不含密钥的错误写到 stderr。

- `bhelp run` 保留被执行命令的退出码。许多程序在打印 `-h` 帮助后本来就会非零退出，例如部分 Git 命令会返回 129；这不代表文本捕获失败。
- 找不到命令时返回 127，命令存在但无法执行时返回 126。
- `man` 页面不存在、配置无效或无法取得任何文本时返回非零。
- 默认降级输出原文不把翻译故障变成硬失败；`--strict` 可用于脚本，让翻译失败返回 3。
- CLI 用法或配置错误返回 2。
- Ctrl+C 会终止等待中的子进程或网络请求并返回 130。输出管道被 `head` 等程序提前关闭时会按 SIGPIPE 正常结束，不打印 Python traceback。

## 卸载

先移除 Git viewer 集成。如果使用首选安装脚本安装，删除 `~/.local/bin` 中的单文件程序；工作区构建产物也可以一并删除：

```bash
bhelp integrate git remove
bhelp cache clear  # 可选
rm -f ~/.local/bin/bhelp
rm -f dist/bhelp  # 可选；请在 bhelp 仓库根目录执行
```

只有通过 venv/pip 安装时才使用：

```bash
python -m pip uninstall bhelp-cli
```

配置和缓存默认保留，方便再次安装。确认不再需要后，可以选择删除：

```bash
rm -f ~/.config/bhelp/config.json
rmdir ~/.config/bhelp ~/.cache/bhelp/v2 ~/.cache/bhelp 2>/dev/null || true
```

这些删除是不可恢复的；如配置文件中保存了仍在使用的密钥，请先确认其他程序不依赖它。卸载本地文件不会撤销云端密钥，密钥轮换仍需在 DeepSeek/OpenAI 控制台完成。

## 当前设计原则

- 始终优先保证原文可读；翻译失败不能让 `git help` 失效。
- 按语义块翻译并用稳定 ID 校验返回结果，避免长手册错位。
- 不使用 `shell=True`、`eval` 或字符串拼接执行用户命令。
- TTY 下友好分页，非 TTY 下保持可组合的纯文本行为。
- 配置、凭据和缓存只使用用户目录，不读取当前项目中的不受信任配置。

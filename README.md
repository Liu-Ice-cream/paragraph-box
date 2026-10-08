# 段落匣 · ParagraphBox

一个面向笔记、写作素材和工作资料的 Flow Launcher Python 插件。`sn 关键词` 搜索本地 Markdown/TXT 的独立段落，返回命中片段、文件名、章节标题与行号。默认只使用随项目附带的人工样例。

当前版本 **0.2.5 · 早期预览版**。仓库从已核验的源码快照建立全新 Git 历史。支持中文、多词与目录别名检索，可视化管理资料目录与排除规则，宽阅读面板查看前后文，并复制带出处的摘录。可选用 VS Code 请求定位到命中行。

图标、设置页与附带的 `ParagraphBox Mint` Flow 主题使用统一的浅色绿色风格。版本变化见 [更新记录](CHANGELOG.md)，原生 Flow 快捷键、编辑器精确行号和系统粘贴尚有待验收项目，见 [验收记录](docs/verification-0.2.5.md)。

![段落匣宽阅读面板与实际工作样例](docs/paragraphbox-reader.png)

## 运行环境

- Windows，Python 3.10 或更高版本；插件无第三方 Python 依赖。
- 单独安装 [Flow Launcher](https://github.com/Flow-Launcher/Flow.Launcher/releases)。已核验的集成版本为 2.1.4。
- 在 Flow 的插件设置中配置可用的 Python 解释器。仓库和插件 ZIP 不包含 Python 或 Flow 主程序。

## 先运行样例

下载或克隆本仓库，在项目根目录打开 PowerShell，运行：

```powershell
python main.py --check
python main.py --query "雨夜"
python main.py --query "@写作 车站 雨夜"
python main.py --query "@工作 JSON 协议"
python main.py --index
python settings_ui.py
python -m unittest discover -s tests -v
```

`--query` 只输出结果，不打开文件或修改剪贴板。CLI 的 JSON 使用 Unicode 转义；PowerShell 可用 `(python main.py --query "雨夜" | ConvertFrom-Json).result | Format-List Title,SubTitle` 阅读中文结果。Flow 会自动解码这些转义。

## 在 Flow Launcher 中安装

从 [Releases](https://github.com/Liu-Ice-cream/paragraph-box/releases) 下载对应版本的插件 ZIP，或从源码生成本地插件包：

```powershell
python scripts/package_plugin.py
```

脚本会输出 ZIP 的完整路径。在已安装的 Flow 中输入以下命令，将占位符替换为实际 ZIP 路径：

```text
pm install <ZIP文件的完整路径>
```

如果插件管理器的联网刷新失败，可使用官方开发安装方式：在 Flow 输入 `userdata`，打开其中的 `Plugins` 目录，建立 `ParagraphBox-0.2.5` 子目录，将 ZIP 的内容解压到该目录，确保 `plugin.json` 直接位于子目录下。在 Flow 输入 `Restart Flow Launcher`。同一个插件 ID 只能有一份；更新旧版时直接覆盖旧安装目录并保留 `config.json`，不要额外创建另一份。

Flow 的 Python 解释器与命令行解释器可能不同；确认 Flow 插件设置中的 Python 可运行 3.10+。本项目没有需要 pip 安装的库。

安装后使用：

```text
sn 雨夜
sn @写作 车站 雨夜
sn @工作 JSON 协议
```

默认回车用 Windows 关联程序打开原文件；未设置 Markdown/JSON 文件关联时会使用记事本，默认打开方式不会跳到行号。在“检索偏好”选择并保存“VS Code · 定位到命中行”后，Flow 回车和阅读面板会请求在 VS Code 中定位到段落起始行。Flow 2.1.4 默认按 `Ctrl+O` 打开上下文菜单，可复制段落、文件路径或带出处的摘录；选择 VS Code 后菜单也提供定位入口。快捷键可以在 Flow 设置中修改。

VS Code 是可选打开方式；本机已有安装时自动检测，无须把它加入 Python 依赖。使用明确的可执行文件和参数启动，不执行自定义命令模板。若未找到 VS Code，会提示在检索偏好中切回系统默认程序。定位前立即检查文件、章节、正文和行号，已变化的段落需重新检索；无法读取、移走或被排除的资料会拒绝打开。配置缺少 `editor` 时按 `default` 处理，可选值为 `default`、`vscode`。更改打开方式不要求重建索引。

## 配置真实资料目录

输入 `sn`，选择“配置检索目录”，在可视化页面里添加文件夹与别名，然后点击“保存更改”。右侧可以直接试搜，并复制摘录。保存时会检查目录是否存在；输入错误不会覆盖之前的配置。设置页仅运行在本机回环地址，打开入口会生成会话凭据，闲置 30 分钟后自动退出；关闭后可从 Flow 再次打开。

试搜结果的“阅读段落”打开宽阅读面板，显示同一文件中前一段、命中段落和后一段，命中段落保留全文高亮、文件名、章节和行号。长内容在正文区滚动，打开与复制按钮始终位于底部；按 Esc 或关闭按钮返回原预览，焦点恢复到阅读入口。前后文按解析后的资料段落计算，Markdown 标题与代码围栏不会作为独立段落；文件开头或结尾有明确提示。每次打开面板会立即检查文件变化，加载期间禁用动作；段落已变化时提供重新检索入口。修改时间和大小都未改变的内容仍需重新建立索引。“复制带出处的摘录”始终仅复制命中段落，不包含前后文。

展开“本次检索”可查看资料准备、索引检查、资料匹配、结果排序、结果整理与完成处理耗时，以及匹配和返回数量。计时来自服务内实际执行，不包含启动 Python 插件进程、响应传输和页面显示；索引检查包含同步与提交时间，完成处理包含关闭索引资源，与上次索引阶段记录的边界不同。

页尾“编辑配置文件”保留手动编辑入口。也可复制 `config.example.json` 为 `config.json` 手动编辑：

```json
{
  "roots": [
    { "alias": "写作", "path": "samples/writing", "exclude_dirs": ["备份", "导出/旧版"] },
    { "alias": "工作", "path": "samples/work" }
  ],
  "max_results": 30,
  "max_file_bytes": 2000000
}
```

示例使用随项目附带的人工资料；将 `path` 替换为你选择的实际目录即可。Windows 路径使用 `/`，或把反斜杠写成 `\\`。相对路径以配置文件所在目录为基准；支持 `%USERPROFILE%` 等环境变量。修改配置后，下一次非空搜索即生效，无需修改源码。

展开每个资料目录的“排除子目录”，每行填写一个相对该资料目录的路径。例如 `备份` 排除该目录下的备份文件夹及其内容，但不会排除 `备份2` 或其他层级同名的目录；`导出/旧版` 只排除指定层级。不支持通配符、绝对路径和 `..`。规则以各个资料目录为单位生效；若另行配置被排除的子目录为独立资料目录，它仍会按自己的规则检索。保存规则后，下一次搜索会清除相应旧结果。

“索引状态”显示上次完成时间、更新耗时、跳过文件数和实际遇到的排除路径入口数。排除一个目录只计一处入口，不递归统计其中全部文件。跳过原因最多展示 50 项，总数仍完整统计。配置修改后会显示“待更新”；这些状态来自上次扫描。连续搜索复用最近 2 秒的已完成扫描，缓存到期后的下一次搜索检查文件变化；可见更新还需完成本次扫描和检索。目录、别名、排除规则或大小上限修改不等待缓存到期。

“重新建立索引”重新读取已保存目录中的全部支持文件，可修复修改时间和大小不变的内容变化。发生数据库或未处理的运行错误时事务回滚，原索引和上次完成记录保留；无法读取、过大或无效文本等文件级问题会被跳过并单独提示。设置页首次索引和重建期间显示实际阶段、已检查文件数和总经过时间，不显示估算百分比。完成后显示本次更新与移除数量。展开“查看上次更新耗时”可了解目录检查、读取、段落整理、数据库操作和等待的耗时；此记录不含设置服务启动、查询排名或最后提交，因此与实时显示的总时间可能不同。设置页升级后请重新打开，以连接当前版本的设置服务。

## 首版检索规则与边界

- 支持 `.md`、`.markdown`、`.txt`，扩展名不区分大小写；空行分段，保留段内换行和从 1 开始的行号。
- 中文使用子串匹配；空格分隔的多个词需全部命中同一段落的正文或章节标题。英文忽略大小写，并归一化全角字符；不做拼音、分词、模糊或语义搜索。
- Markdown 识别 ATX 与 Setext 标题，显示章节层级；不把标题、开头的 YAML front matter、代码围栏或分隔线当作段落。TXT 按纯文本处理。
- 支持 UTF-8、带 BOM 的 UTF-8/UTF-16，以及常见 GB18030 中文文件；无法解码、过大或不可读的文件会跳过并提示。
- 搜索复用最近 2 秒的已完成扫描，到期后再次搜索检查配置目录的文件状态；配置改变立即检查，`python main.py --index` 和手动重建不复用扫描。仅重新解析新增或修改的文件；已删除、被排除的文件会从索引移除。修改时间和字节数同时不变的内容变化可以通过设置页“重新建立索引”刷新。
- SQLite 索引保存于插件目录的 `.cache`，内容包含段落原文；日志是 `.cache/plugin.log`。`config.json` 与缓存均被 Git 忽略，也不会进入安装 ZIP。
- 目录扫描不跟随内部符号链接或 Windows 目录联接。大型资料库仍需进一步优化目录扫描和排名；本版未覆盖 PDF、标签或向量检索。

## Flow 窗口主题

将插件里的 `Themes/ParagraphBox Mint.xaml` 复制到 Flow 的 `UserData/Themes`，在 Flow 设置的主题列表中选择 `ParagraphBox Mint`。建议搜索框宽度 750、结果高度 78、输入字号 22、结果字号 16、出处字号 12。

主题会作用于该 Flow 实例的整个搜索窗口；段落匣的独立设置页保持自己的样式。Flow 本身的完整设置窗口仍由主程序提供。

## 代码入口

`main.py`：官方经典 JSON 请求入口和本地 CLI；`paragraph_search/engine.py`：段落解析、增量 SQLite 索引和关键词排名；`paragraph_search/plugin.py`：Flow 结果、上下文菜单与打开文件；`paragraph_search/settings.py`：配置验证；`settings_ui.py` 和 `paragraph_search/settings_server.py`：本地设置服务；`ui/`：设置界面；`Themes/`：Flow 主题。

协议只分派明确支持的方法，stdout 只输出协议 JSON，Flow 模式不写 stderr。复制菜单使用 `Flow.Launcher.CopyToClipboard`，显式提供三个参数。打开文件动作返回空 stdout，与经典 Python SDK 的动作行为一致。未使用第三方 SDK，因此升级接口时应对照官方源码复核。

作品显示名为“段落匣 · ParagraphBox”，仓库为 [Liu-Ice-cream/paragraph-box](https://github.com/Liu-Ice-cream/paragraph-box)。这是独立的 Flow Launcher 插件。

2026-10-04 检索 GitHub 公开仓库名称与网页，未发现完全同名的 `ParagraphBox`、`paragraph-box` 或“段落匣”项目；英文词在其他软件中存在类名或排版术语用途。这是公开项目名称检索结果，不代表全网唯一性。

作者为 `Liu-Ice-cream`，项目使用 [MIT 许可证](LICENSE)。Flow Launcher 是独立的上游项目，需单独安装，其代码与许可不包含在本插件包中。

## 本地数据与分享

`config.json` 保存所选资料目录，`.cache` 内的 SQLite、日志和设置服务状态可能包含文件路径、段落原文或会话凭据。这些本地文件不会提交，也不会进入插件包。分享时使用 `scripts/package_plugin.py` 生成的 ZIP；脚本只打包明确列出的项目文件，额外放入 `docs`、`samples` 等目录的文件也不会自动进入 ZIP。

截图和默认资料均为人工样例。为他人报告问题时，也应使用人工样例，避免附上真实资料、日志、配置、索引或整个工作目录。公开发布整理情况见 [发布检查记录](docs/publication.md)。

## 官方参考

- [Python 示例模板](https://github.com/Flow-Launcher/Flow.Launcher.Plugin.HelloWorldPython)
- [Python 开发文档](https://github.com/Flow-Launcher/docs/blob/main/py-write-code.md)
- [本地测试与安装](https://github.com/Flow-Launcher/docs/blob/main/testing.md)
- [v2.1.4 经典 JSON RPC 接口](https://github.com/Flow-Launcher/Flow.Launcher/blob/v2.1.4/Flow.Launcher.Core/Plugin/JsonRPCPlugin.cs)
- [v2.1.4 公共接口（含剪贴板方法）](https://github.com/Flow-Launcher/Flow.Launcher/blob/v2.1.4/Flow.Launcher.Plugin/Interfaces/IPublicAPI.cs)

## 版本与验收

查看 [更新记录](CHANGELOG.md)。0.2.5 可以直接覆盖旧版安装目录，插件 ID 保持一致，安装包不包含个人 `config.json` 与索引缓存。旧配置和索引不需要迁移。阅读面板、编辑器、升级及原生操作边界见 [0.2.5 验收记录](docs/verification-0.2.5.md)，检索性能见 [0.2.4 验收记录](docs/verification-0.2.4.md)。原生窗口控制受限的项目在记录中明确标为待验收。

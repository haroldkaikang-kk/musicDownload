# MusicDownload master 4e46b11 功能对照表

## 1. 核对范围与判定规则

- 原版基线：`master` 提交 `4e46b11c0eec5b8f2d5947da07af0d7997005c2d`（下文简称 `master 4e46b11`）。
- 目前基础分支：`MusicDownload-1.3.0-UI-Edition-macOS12-Intel`，核对时 HEAD 为 `a8a606f`，程序内版本仍为 1.2.1。
- 本表只依据上述两个 Git 对象中的代码和配置文件判定。代码能证明入口或处理逻辑存在，但不能证明网络音源、音频设备、打包后的资源和 macOS 权限在正式 App 内必然可用；这类项目统一标记为“待实机验收”。
- 状态含义：`已具备`＝代码中存在明确入口和处理逻辑；`部分具备`＝只覆盖需求的一部分；`未具备`＝未找到对应实现；`待实机验收`＝静态代码不能证明实际运行结果。

## 2. 不可变产品决策

1. V1.3.0 首次启动不预设勾选任何音源，不恢复 `master 4e46b11` 默认勾选酷我音乐、酷狗音乐的行为。
2. 单曲“立即下载”和“加入下载队列”两个入口都必须保留，含义不得合并。
3. 结果区上方与右键菜单都必须保留“全选”和“取消全选”。
4. 删除“下载未勾选歌曲”功能，同时删除“下载范围”下拉框；主下载按钮只处理已勾选歌曲。
5. `master 4e46b11` 的既有能力不得无故消失；若交互位置调整，必须在正式 App 中提供等价且可发现的入口。
6. 目前基础分支新增的进程隔离、硬超时、安全取消、真实音频参数、完整解码、历史、重复管理、旋转日志和 Intel Mac 构建能力必须保留。

## 3. 逐项功能对照

| 功能 | 原 master 4e46b11 | 目前基础分支 | V1.3.0 最终要求 | 代码依据 | 验收方式 | 是否需要正式 App 实测 |
|---|---|---|---|---|---|---|
| 1. 17 个音乐来源 | 已具备：定义 17 个来源 | 已具备：仍为 17 个，5sing 名称更明确 | 原样保留 17 个并全部可选 | master：`musicdownload.py` `MusicDownloader.__init__.source_map_cn_to_en`；当前：`musicdownload_core.py` `SOURCE_DEFINITIONS` | 自动检查常量长度为 17、ID 唯一；逐源搜索一次 | 是：需验证各来源实际返回状态 |
| 2. 启动时默认音源 | 默认勾选酷我、酷狗 | 首次启动不勾选；开启“记住音源”后可恢复用户选择 | 首次启动必须全不选；不得恢复原版酷我/酷狗默认值 | master：`MusicDownloader.setup_top` 中 `default_checked`；当前：`MusicDownloader.setup_sources` 中 `remember_sources`、`saved_sources` | 清空 QSettings 后启动，17 个复选框均未选；再次启动按记忆选项验证 | 是 |
| 3. 歌曲搜索 | 已具备 | 已具备，并改为逐来源隔离搜索 | 保留，支持单源和多源 | master：`SearchThread.run` 调用 `MusicClient.search`；当前：`SearchCoordinator._run_search`、`_worker_search` | 输入关键词，验证每个所选来源有独立状态且结果可显示 | 是：依赖网络与上游接口 |
| 4. 歌单链接解析 | 已具备 | 已具备 | 保留，并与歌曲搜索同级可见 | master：`SearchThread.run` 调用 `parseplaylist`；当前：`MusicDownloader.start_search`、`musicdownload_core._worker_search` | 切换“解析歌单链接”，输入有效链接，验证结果 | 是：依赖链接与上游接口 |
| 5. 搜索模式切换 | 已具备 | 已具备 | “搜索歌曲／解析歌单链接”在正式 App 中可见、可选、可用 | master：`MusicDownloader.setup_top.search_mode`；当前：`MusicDownloader.setup_search_controls.search_mode` | 切换后检查占位文案、请求路径和结果 | 是 |
| 6. 每个来源的搜索数量 | 已具备，1–100，默认 10 | 已具备，1–100，默认 10 | 保留可调，并按单来源生效 | master：`spin_limit`、`init_music_client`；当前：`spin_limit`、`_worker_search` 中 `search_size_per_source` | 设为 1、10、100，检查传入每个来源的配置及返回上限 | 是：上游可能少于设定值 |
| 7. 保存目录显示 | 已具备，只读显示 | 已具备，只读显示 | 始终显示当前实际保存位置 | master：`save_dir_edit`；当前：`save_edit`、`browse_save_dir` | 修改目录后检查显示值与实际输出父目录一致 | 是 |
| 8. 保存目录直接输入 | 未具备：输入框只读 | 未具备：输入框仍只读 | 必须可直接输入或粘贴 | 两版均在 `setup_top`／`setup_search_controls` 调用 `setReadOnly(True)` | 直接输入有效路径并失焦/确认，值被接纳和验证 | 是 |
| 9. 保存目录浏览按钮 | 已具备 | 已具备 | 保留“浏览…”选择目录 | master：`on_browse_save_dir`；当前：`browse_save_dir` | 选择目录、取消选择、再次打开时检查初始位置 | 是：系统文件对话框 |
| 10. 保存路径验证 | 部分具备：浏览框返回现有目录，下载前 `makedirs`；无可写性检查 | 部分具备：启动时尝试创建并回退；无直接输入和显式可写性检查 | 验证存在、可创建、可写；无效路径必须阻止保存并解释原因 | master：`init_music_client`；当前：`MusicDownloader.__init__`、`browse_save_dir` | 分别测试存在可写、不存在可建、只读、非法路径 | 是：权限必须实测 |
| 11. 搜索后自动下载 | 已具备：勾选后直接下载全部 | 已具备：勾选后打开队列并 `auto_start=True` | 作为三态偏好之一“自动加入并开始下载” | master：`load_table_with_results`；当前：`search_completed`、`open_download_queue` | 搜索完成后自动生成队列并开始，未选择该项时不得开始 | 是 |
| 12. 搜索后自动加入队列 | 未具备 | 未独立具备：现有复选框会同时开始下载 | 增加“不自动／仅加入队列／加入并开始”三态 | 当前：`auto_download` 文案与 `search_completed(auto_start=True)` 显示行为不一致 | 选择“自动加入下载队列”，搜索后队列出现但保持等待 | 是 |
| 13. 单曲立即下载 | 已具备：右键确认后直接执行 | 未具备：右键只有加入队列 | 必须恢复并与“加入下载队列”并列保留 | master：`show_table_context_menu`、`download_current_row`；当前：`show_result_menu` 仅 `open_download_queue` | 右键单曲点“立即下载”，不进入等待队列并立即处理该曲 | 是 |
| 14. 单曲加入下载队列 | 未具备 | 已具备 | 必须保留 | 当前：`show_result_menu` 的“加入队列”、`open_download_queue([song])` | 右键加入后状态为等待，用户点击开始前不得下载 | 是 |
| 15. 批量下载 | 已具备 | 已具备：通过下载队列 | 只对已勾选项目批量加入队列 | master：`on_download`；当前：`add_selected_to_queue`、`DownloadQueueDialog` | 勾选多首，按钮计数正确，队列条目与顺序一致 | 是 |
| 16. 勾选歌曲 | 已具备 | 已具备，并用稳定 track key 保留筛选前后的选中状态 | 两种结果视图都可勾选；切换视图不丢选择 | master：结果表 checkbox；当前：`checked_track_keys`、`update_checked_key` | 勾选、排序、筛选、切换视图后核对同一歌曲仍选中 | 是 |
| 17. 全选 | 已具备：右键全选；下载范围另有“全选” | 已具备：结果上方“勾选全部可见”；右键缺失 | 结果上方和右键菜单都保留“全选” | master：`select_all_songs`；当前：`set_visible_checks(True)` | 两个入口分别触发，当前可见结果全部勾选 | 是 |
| 18. 取消全选 | 已具备：右键取消全选 | 已具备：结果上方“取消全部可见”；右键缺失 | 结果上方和右键菜单都保留“取消全选” | master：`deselect_all_songs`；当前：`set_visible_checks(False)` | 两个入口分别触发，当前可见结果全部取消 | 是 |
| 19. 未勾选项下载 | 已具备 | 已具备 | V1.3.0 必须删除，不得保留等价隐藏入口 | master：`get_songs_by_download_scope`；当前：`download_scope`、`songs_for_scope` | 全仓搜索和正式 App 均无“未勾选”下载方式 | 是 |
| 20. 右键单曲下载 | 已具备，行为是立即下载 | 部分具备：只有“加入队列” | 右键同时提供“立即下载”和“加入下载队列” | master：`show_table_context_menu`；当前：`show_result_menu` | 逐一执行两个菜单项，确认行为不同 | 是 |
| 21. 右键全选 | 已具备 | 未具备 | 恢复 | master：`show_table_context_menu`；当前菜单未添加对应 QAction | 右键任一结果后可全选当前可见结果 | 是 |
| 22. 右键取消全选 | 已具备 | 未具备 | 恢复 | master：`show_table_context_menu`；当前菜单未添加对应 QAction | 右键任一结果后可取消当前可见结果 | 是 |
| 23. 封面显示 | 已具备，后台线程池加载 | 已具备，后台线程池加载 | 两种结果视图和播放器均显示封面，失败时使用正式占位图标 | master：`ImageDownloadTask`、`on_image_downloaded`；当前：`ImageDownloadTask`、`cover_loaded` | 有封面、无封面、超时、无效图片四种情况 | 是：远程图片与正式资源 |
| 24. 歌曲名称 | 已具备 | 已具备 | 现代列表、专业表格、队列、历史、播放器都显示 | master：`load_table_with_results`；当前：`song_title` 及各表构建函数 | 对照原始结果对象核对文本 | 是 |
| 25. 歌手 | 已具备 | 已具备 | 同上 | master：`load_table_with_results`；当前：`song_artist` | 多歌手、缺失歌手、特殊字符 | 是 |
| 26. 专辑 | 已具备 | 已具备 | 现代列表与专业表格显示 | master／当前：结果表填充函数 | 有专辑、空专辑 | 是 |
| 27. 格式 | 已具备：按字段或 URL 推断 | 已具备：搜索结果推断，下载后按真实文件分析 | 搜索阶段标明“来源声明/推断”；下载后显示实测格式 | master：`get_file_format`；当前：`search_format`、`analyze_audio` | 对比搜索元数据与下载后文件 | 是 |
| 28. 文件大小 | 已具备 | 已具备 | 两种结果视图按可用数据显示；未知时明确“未知” | master：`NumericTableItem`；当前：`parse_file_size_bytes`、`populate_results` | B/KB/MB/未知格式与排序 | 是 |
| 29. 时长 | 已具备 | 已具备 | 两种结果视图显示并可排序 | master：`extract_numeric_value`；当前：`format_duration`、`song_duration_seconds` | mm:ss、hh:mm:ss、未知和排序 | 是 |
| 30. 来源 | 已具备 | 已具备 | 两种结果视图、队列、历史显示中文来源名 | master：`source_map_en_to_cn`；当前：`SOURCE_EN_TO_CN`、`source_label` | 17 个 ID 与显示名逐一映射 | 否：映射可自动核对；来源可用性另测 |
| 31. 采样率 | 未具备 | 已具备：搜索表显示来源值，下载报告/历史显示实测值 | 专业表格显示；下载后用真实文件值更新或明确区分 | 当前：`populate_results`、`analyze_audio`、`HistoryDialog` | 用已知 44.1/48/96 kHz 文件核对 | 是 |
| 32. 位深 | 未具备 | 部分具备：下载报告和历史有，搜索结果表无 | 专业表格显示；有损格式不得伪造 PCM 位深 | 当前：`analyze_audio` 清除有损位深、`HistoryDialog` | 16/24-bit 无损与 MP3/AAC 对照 | 是 |
| 33. 比特率 | 未具备 | 已具备：搜索表及下载后实测 | 专业表格显示并支持排序 | 当前：`format_bitrate`、`analyze_audio`、`filter_sort_songs` | 128/320 kbps、VBR、未知值 | 是 |
| 34. 编码 | 未具备 | 部分具备：报告和历史显示，搜索结果表无 | 专业表格显示；未知时明确标记 | 当前：`display_codec`、`analyze_audio`、`HistoryDialog` | FLAC/ALAC/AAC/MP3/PCM 等文件核对 | 是 |
| 35. 声道 | 未具备 | 部分具备：报告和历史显示，搜索结果表无 | 专业表格显示 | 当前：`analyze_audio`、`HistoryDialog` | 单声道、双声道、多声道文件核对 | 是 |
| 36. 音乐播放 | 未具备 | 部分具备：QMediaPlayer“试听”入口 | 完整播放器；全部“试听”文案改为“播放” | 当前：`media_player`、`preview_song`、`preview_selected_row` | 在线 URL、本地历史文件、双击、右键和播放按钮 | 是：媒体后端与音源 URL |
| 37. 播放停止 | 未具备 | 已具备“停止试听” | 底部播放器固定显示“停止”，停止后时间与状态复位 | 当前：`stop_preview` | 播放中点击停止，确认音频和进度停止 | 是 |
| 38. 播放进度 | 未具备 | 未具备 | 显示当前/总时长，可拖动定位 | 当前未连接 `positionChanged`、`durationChanged`，未创建进度控件 | 播放中观察进度，拖到 25%/75% 核对定位 | 是 |
| 39. 音量控制 | 未具备 | 部分具备：内部固定为 0.7，无界面 | 提供音量滑杆与静音 | 当前：`QAudioOutput.setVolume(0.7)` | 调 0/50/100%，静音/恢复，检查状态保持 | 是 |
| 40. 音质筛选 | 未具备 | 部分具备：全部、无损优先、仅 FLAC、MP3 320k 优先，混合了筛选与排序 | 独立筛选为全部/无损/高品质有损/一般有损/未知 | 当前：`quality_filter`、`search_quality_rank`、`filter_sort_songs` | 构造各类别数据并核对包含/排除 | 是 |
| 41. 来源筛选 | 未具备 | 已具备动态来源列表；单一来源时仍显示“全部来源” | 按实际结果动态生成；仅一个来源时禁用或隐藏 | 当前：`refresh_source_filter` | 0、1、2、17 个实际来源结果分别验证 | 是 |
| 42. 排序 | 已具备：点击表头排序 | 已具备：下拉排序及表头排序；缺少时长下拉项 | 排序与音质筛选分开；覆盖默认、音质、采样率、比特率、大小、时长、来源 | master：`setSortingEnabled`、`NumericTableItem`；当前：`sort_combo`、`filter_sort_songs` | 每个排序项用已知数据核对顺序及稳定性 | 是 |
| 43. 命名规则 | 部分具备：固定“歌名-歌手-专辑-identifier” | 已具备 4 个命名模板 | 恢复并保留全部已定义模板，固定原版信息不得无故丢失 | master：`DownloadThread.run`；当前：`NAMING_TEMPLATES`、`current_naming_template` | 每个模板下载一首，核对目录和文件名 | 是 |
| 44. 自定义命名模板 | 未具备 | 已具备输入和保存 | 保留，校验变量与语法 | 当前：`SettingsDialog.custom_naming`、`render_output_path` | 合法、空、未知变量、目录穿越输入 | 是 |
| 45. 命名预览 | 未具备 | 未具备 | 输入或切换模板时即时预览，不写文件 | 当前未见预览控件或 change signal | 改模板后 200 ms 内预览更新，非法项显示原因 | 是 |
| 46. 下载队列 | 未具备 | 已具备弹窗队列 | 改为主界面“下载队列”页签，同时保留完整队列能力 | 当前：`DownloadQueueDialog` | 加入单曲/多曲、切页、窗口重开，队列不丢失 | 是 |
| 47. 下载暂停与继续 | 未具备 | 已具备：当前歌曲完成后暂停 | 保留，界面明确暂停生效边界；继续后从等待项继续 | 当前：`DownloadCoordinator.set_paused`、`_wait_while_paused`、`toggle_pause` | 下载多首时暂停，核对当前项和下一项行为 | 是 |
| 48. 下载取消 | 未具备 | 已具备安全取消当前进程和剩余队列 | 保留；不留孤儿进程和半成品 | 当前：`DownloadCoordinator.cancel`、`IsolatedWorkerJob.terminate` | 下载中取消，检查进程、临时目录、队列状态 | 是 |
| 49. 失败项重试 | 未具备 | 已具备：报告中只重试失败/超时/异常 | 主队列页签提供“重试失败项” | 当前：`DownloadReportDialog.retry_requested`、`DownloadQueueDialog.retry_items` | 制造失败后只重试失败项，成功项不重复 | 是 |
| 50. 下载历史 | 未具备 | 已具备 SQLite 下载历史弹窗；无搜索历史 | 主界面提供下载历史页签，并按需求加入搜索历史 | 当前：`HistoryStore`、`HistoryDialog` | 成功/失败/超时记录持久化，重启后仍存在 | 是 |
| 51. 重复文件处理 | 部分具备：同名目标直接删除覆盖，无管理策略 | 已具备询问、跳过、保留两份、替换并移废纸篓 | 保留当前安全策略；替换只在确认后用危险色 | master：`DownloadThread.run` 的 `os.remove`；当前：`find_existing`、`on_duplicate_request`、`finalize_download` | 四种策略逐一测试，检查原文件和历史 | 是 |
| 52. 真实音频参数检查 | 未具备 | 已具备 | 保留真实格式、编码、采样率、位深、比特率、声道、时长、大小 | 当前：`musicdownload_core.analyze_audio` | 用已知媒体样本及独立工具对照 | 是 |
| 53. 完整音频解码检查 | 未具备 | 已具备：PyAV 遍历全部音频帧 | 保留；只读元数据不得判定为完整解码成功 | 当前：`analyze_audio` 中 `for frame in container.decode(stream)` | 正常文件、截断文件、零帧文件 | 是 |
| 54. 音源隔离 | 未具备：多个来源在同一 MusicClient/QThread | 已具备：每个来源独立子进程 | 必须保留，单一来源崩溃不影响其他来源 | 当前：`SearchCoordinator._run_search`、`IsolatedWorkerJob`、`_worker_search` | 注入一个崩溃/挂起来源，其他来源仍结束 | 是 |
| 55. 搜索硬超时 | 未具备；封面请求仅有 5 秒软请求超时 | 已具备每来源硬超时并终止进程 | 必须保留且可配置 | 当前：`SearchCoordinator.timeout_seconds`、`job.terminate`；`SettingsDialog.search_timeout` | 模拟挂起超过阈值，进程被终止并标“超时” | 是 |
| 56. 下载硬超时 | 未具备 | 已具备每歌曲硬超时，覆盖下载和完整解码 | 必须保留且可配置 | 当前：`DownloadCoordinator._run_queue`、`download_timeout` | 模拟下载/解码挂起，超时后继续下一项 | 是 |
| 57. 日志 | 未具备：只向标准输出打印 | 已具备 2 MiB×3 的 UTF-8 旋转日志 | 保留，偏好设置提供历史与日志入口 | 当前：`setup_logging`、`RotatingFileHandler`、`LOG_DIR` | 触发日志轮转，验证编码、数量和错误记录 | 是：正式 App 沙盒路径 |
| 58. 浅色模式 | 已具备固定浅色样式 | 已具备可选浅色 | 默认使用柔和浅色 | master：`get_modern_style`；当前：`apply_theme('light')` | 首次启动视觉、对比度、控件状态 | 是 |
| 59. 深色模式 | 未具备 | 已具备 | 作为可选项，并新增“跟随系统” | 当前：`apply_theme`、`theme_actions` | 浅/深/跟随系统切换和重启保持 | 是 |
| 60. 偏好设置 | 未具备 | 部分具备：单页“专业设置”弹窗 | 建立 7 个正式分页；无底层功能的选项不得出现 | 当前：`SettingsDialog`、`show_settings` | `Command+,` 打开；逐页保存、取消、恢复默认 | 是 |
| 61. macOS 12 Intel 打包 | 未具备明确基线：仅通用 PyInstaller 命令 | 代码配置已具备：x86_64、最低 12.0、Intel 检查、DMG；正式包结果待验 | V1.3.0 在 macOS 12.7.6 Intel x86_64 构建、签署、启动和运行 | master：`make_release.sh`；当前：`MusicDownload.spec`、`制作正式版.command` | 在目标机生成 App/DMG，检查 `file`、Info.plist、codesign 并启动 | 是：必须目标机正式包实测 |
| 62. 独立 1.2.1／1.3.0 `.venv` | 未具备 | 部分具备：当前脚本强制 1.2.1 使用自身 `.venv`，尚无 1.3.0 版本脚本 | 1.2.1 与 1.3.0 各用自己目录内的 `.venv`，不得交叉运行或安装 | 当前：`制作正式版.command` 的 `VENV_DIR`、`运行源码版.command` 的 `VENV_PYTHON` | 同时保留两目录，分别启动并检查 `sys.prefix` 和版本号 | 是 |

## 4. 需要重点防回归的迁移项

| 迁移项 | 原版能力 | 当前缺口 | V1.3.0 验收门槛 |
|---|---|---|---|
| 单曲下载双入口 | 原版右键可立即下载 | 当前只可加入队列 | “立即下载”和“加入下载队列”并列可见，行为不同且均成功 |
| 结果选择入口 | 原版右键有全选/取消全选 | 当前只在结果上方提供 | 上方和右键各有一组，且只作用于当前可见结果 |
| 下载范围 | 两版都有“未勾选” | 与 V1.3.0 决策冲突 | 删除下拉框和“未勾选”；主按钮只处理勾选项并显示数量 |
| 原版字段 | 原版已有封面、歌曲、歌手、专辑、格式、大小、时长、来源 | 当前大体保留 | 两种结果视图均不得少于各自规范字段 |
| 稳定性能力 | 原版没有进程隔离和硬超时 | 当前已新增 | UI 重构后仍通过隔离、超时、取消、解码、历史、重复管理测试 |

## 5. master 4e46b11 仍需实机验收的边界

以下内容仅凭 `master 4e46b11` 代码不能确认实际可用：17 个上游音源在当前网络环境的搜索/歌单解析结果；远程封面加载；真实单曲与批量下载、重命名和歌词移动；排序后右键下载是否始终对应可见行；不同格式的大小/时长显示与排序；PySide6 与 musicdl 组合在目标 macOS 12 Intel 机器上的启动、退出及打包结果。它们在本表中均未被写成“已通过”，必须以正式 App 实测记录关闭。

# 启动、数据目录与 Wallpaper Engine

本项目不会自动修改开机启动项、驱动、Wallpaper Engine 设置或 Steam 设置。下面所有设置均由你自愿执行。源码更新应先在独立目录测试，保留已正常工作的环境。

## 首次启动（Windows）

1. 安装 Python 3.11 或更高版本，然后双击仓库根目录的 `install-dependencies.cmd`。它仅在当前仓库创建 `.venv` 并安装 `requirements.txt` 中的 Python 依赖，不会安装驱动或第三方原生 SDR 库，也不会请求管理员权限。
2. 按设备兼容说明自行安装驱动和对应原生库。演示模式不需要 SDR 设备。
3. 双击 `start.cmd`，在本地控制面板选择模式并启动接收。命令窗口是后台进程，请保持打开；关闭它会停止数据供应。

`start.cmd` 优先使用本目录 `.venv\Scripts\python.exe`，没有虚拟环境时使用 PATH 中的 `python`。它在 `app` 目录运行，不依赖开发者的 Conda 路径。默认参数为 `--open --keep-wallpaper`：打开本地控制面板，后台退出时不恢复或更换桌面。若你在命令行给出参数，这些参数会完整传入并替代默认参数；请在需要时显式保留 `--keep-wallpaper`。

例如，在仓库根目录运行：

```bat
start.cmd --open --keep-wallpaper --auto-start
```

这会立即尝试打开配置的接收设备；声音仍默认关闭。多个后台不能同时占用同一台 SDR；升级时先关闭旧后台，不要同时运行新旧版本。

## 导入网页壁纸

在 Wallpaper Engine 编辑器中按 Web 壁纸导入 `app/original_wallpaper/index.html`，保存并选中这份工程。用 Wallpaper Engine 自身的“开机启动”及壁纸恢复功能管理桌面。后台只提供本机信号数据，不需要先启动 Steam 或代理才能运行。

不要为日常开机启动加入 `--apply-wallpaper`。这个参数及控制面板的临时应用/恢复功能用于明确要求切换桌面的场景，只处理 Monitor0，并保留切换前的恢复记录。它不应替代 Wallpaper Engine 对正式导入工程的管理。

可选的临时切换功能按以下顺序查找 Wallpaper Engine：

1. 环境变量 `INVISIBLE_TERRAIN_WALLPAPER_EXE` 指定的文件。
2. Steam 的 Windows 注册表安装位置及 `libraryfolders.vdf` 内的库目录。
3. 标准 Program Files 位置下的 Steam 库目录。

优先选择 `wallpaper64.exe`，找不到时尝试 `wallpaper32.exe`。显式环境变量若指向不存在的文件，会报告不可用，不会偷偷选择另一份安装。查找和状态检查只读，不会启动 Steam、启动 Wallpaper Engine、改写配置或创建恢复记录。

非标准安装路径可在启动后台前设置，例如（把示例替换成自己的真实路径）：

```bat
set "INVISIBLE_TERRAIN_WALLPAPER_EXE=X:\Games\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe"
start.cmd
```

## 可选：让接收后台开机启动

先确认手动启动和接收正常，再配置；本仓库没有会自动写入启动项的脚本。

1. 按 `Win+R`，输入 `shell:startup`，打开当前用户的启动文件夹。
2. 新建一个快捷方式。目标是你自己的仓库虚拟环境中的 `pythonw.exe`，加上后台脚本的绝对路径和 `--auto-start --keep-wallpaper`。例如：

   ```text
   "X:\Projects\invisible-terrain\.venv\Scripts\pythonw.exe" "X:\Projects\invisible-terrain\app\original_bridge.py" --auto-start --keep-wallpaper
   ```

3. 将快捷方式的“起始位置”设为自己的 `app` 目录。路径只是示例，不要照抄盘符。若配置设备依赖环境变量，应先为当前用户配置好这些变量，再重新登录。
4. 在 Wallpaper Engine 内单独开启自己的启动选项。壁纸启动早于接收后台时会等待本地数据，不需要通过 Steam 启动接收后台。

诊断启动失败时，将快捷方式中的 `pythonw.exe` 暂时换成 `python.exe` 以查看命令窗口，或手动运行 `start.cmd`。移除自己添加的快捷方式即可取消后台开机启动；这不会删除收藏或改变 Wallpaper Engine 的启动设置。

## 个人数据不写入源码目录

默认数据目录为：

- Windows：`%LOCALAPPDATA%\InvisibleTerrain`。
- 其他系统：`$XDG_STATE_HOME/invisible-terrain`，未设置时为 `~/.local/state/invisible-terrain`。这是路径约定，不代表所有桌面、驱动和声音功能已跨平台实机验证。
- 显式设置 `INVISIBLE_TERRAIN_DATA_DIR` 可覆盖以上位置，建议使用绝对路径。

收藏、上次频率、音量及运行恢复状态属于个人数据，不应提交到 GitHub。读取路径和查询状态不会创建目录；写入数据时才创建。迁移收藏时，先退出所有后台，再将旧环境的 `radio_stations.json` 复制到新数据目录；保留原文件作为备份。不要把开发者的示例/测试收藏当作自己的电台列表。声音开关不会被保存为开机自动播放。

如果源码移动到别处，更新自己创建的启动快捷方式以及 Wallpaper Engine 工程位置；不要通过删除恢复日志来强制切换桌面。

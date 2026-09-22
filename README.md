# 电波地形 · Invisible Terrain

把电波变成会留下时间痕迹的山脉。横向是频率，纵深是过去 48 秒，山脊高度是信号相对噪声的强弱。

原创 Canvas 网页视觉 + 本地 Python 接收助手，可在浏览器运行，也可作为 Wallpaper Engine 的 Web 壁纸使用。免费开源，采用 [MIT 许可证](LICENSE)。

## 能做什么

- 实时频谱地形、调频、定格画面与显示尺度调整。
- FM 广播候选扫描、取消扫描、名称/频率筛选、强度排序。
- 收藏、改名、一键调谐；在本机记住收藏、上次频率和音量。
- 可选 FM 收听：87.5–108 MHz、48 kHz 单声道、50 μs 去加重；每次启动声音默认关闭。
- 明确标注的合成演示，不需要 SDR 硬件，不播放广播声音。

本项目仅接收，不发射。搜台结果只是信号候选，不是台名识别或清晰收听保证；不提供 AM、窄带 FM、立体声或 RDS 解码。

## 先运行演示

需要 Python 3.11 或 3.13。以下命令在仓库根目录执行；建议使用独立虚拟环境，并确保 `python` 指向该环境。

```sh
python -m venv .venv
```

激活环境：Windows 命令提示符使用 `.venv\Scripts\activate.bat`；Linux/macOS 使用 `source .venv/bin/activate`。然后：

```sh
python -m pip install -r requirements.txt
python app/original_bridge.py --demo --open --keep-wallpaper
```

打开 [本机控制台](http://127.0.0.1:8766/) 查看合成地形；`--demo` 会明确启动演示。保持后台进程运行，`Ctrl+C` 退出。不带 `--demo` 或 `--auto-start` 时，助手默认等待用户点击接收。浏览器、演示和自动化测试不等于真实设备兼容性验证。

## 接入真实电波

需要兼容 SDR、天线，以及由用户单独安装的设备驱动/接收库。本仓库不附带驱动、DLL 或 SDR++。

```sh
python app/original_bridge.py --backend rtl --list-devices
python app/original_bridge.py --backend rtl --device-index 0 --open --keep-wallpaper
```

在控制台选择“真实 SDR”并启动接收。找不到原生库时，用 `--rtl-library` 指定已安装库的路径。SoapySDR 是可选的实验性接收后端，需要匹配当前 Python 的 SoapySDR 绑定和对应设备模块，详见 [硬件与驱动](docs/HARDWARE.md)。

需要收听时再安装：

```sh
python -m pip install -r requirements-audio.txt
```

重启接收助手后，调到当地已知 FM 电台，低音量开启收听。搜台暂时占用同一接收器，会暂停地形和声音；完成或取消后返回原频率，并恢复此前的收听意图。停止接收不会自动重新开始。

## 用作 Wallpaper Engine 壁纸

在 Wallpaper Engine 编辑器中创建 Web 壁纸，导入 `app/original_wallpaper/index.html` 及同目录资源。壁纸只读展示；调频、声音与收藏通过本机控制台操作。Wallpaper Engine 需另行安装，本项目不包含它。

**仅订阅或导入网页不会安装、运行 Python 助手。** 目前连合成演示也由助手提供。无后台时显示等待状态/网格；已有地形会停留，不会冒充实时电波。默认连接地址为 `127.0.0.1:8766`，壁纸使用时请保留默认端口。

先分别验证助手和壁纸，再自行配置开机启动；不要同时配置多个接收助手。公开版不替用户修改驱动或 Wallpaper Engine 设置。

## 兼容性状态

RTL-SDR Blog V4 曾在 Windows 上验证真实接收、FM 收听与搜台；公开版重构后的原生 RTL 适配器仍待实机回归。SoapySDR 当前仅有模拟驱动契约测试，**尚无真实硬件验证**。不能据此宣称支持所有 SoapySDR 设备。

Wallpaper Engine 2.7.0.3 在一台 Windows 机器上有稳定运行记录；当前发行版本的兼容性回归仍待进行，不保证所有版本/显卡/启动环境都正常。项目不要求或自动执行版本回退。详见 [硬件状态与报告方法](docs/HARDWARE.md)。

## 开发与贡献

```sh
python -m pip install -r requirements-audio.txt
python -m unittest discover -s app -p "test_*.py"
node app/test_original_model.cjs
node app/test_original_audio_ui.cjs
node app/test_original_stations_ui.cjs
```

Node.js 22 仅用于测试，正常运行不需要 Node 或前端打包工具。CI 配置覆盖 Windows/Ubuntu、Python 3.11/3.13；模拟测试不访问真实 SDR、切换桌面或播放声音。

请阅读 [贡献指南](CONTRIBUTING.md)、[隐私说明](docs/PRIVACY.md) 与 [第三方依赖说明](THIRD_PARTY_NOTICES.md)。欢迎带版本信息的硬件反馈；不要上传设备序列号、收藏、个人目录或未经脱敏的日志。

## English overview

Invisible Terrain is an original procedural radio landscape: frequency across the screen, time into the distance, and relative signal strength as mountain ridges. It pairs a local Python receiver with a Canvas web view, optionally used as a Wallpaper Engine web wallpaper.

Features include tuning, candidate FM scanning, filtering, named favorites, local frequency/volume memory, and optional 48 kHz mono WBFM listening. Audio starts disabled. Reception only; no transmission. A clearly labeled synthetic demo runs through the same local companion, without hardware.

The public native RTL adapter awaits hardware regression testing; the optional SoapySDR adapter is experimental and currently tested with fake drivers only. Drivers and binaries are not bundled. See [hardware setup](docs/HARDWARE.md), [privacy](docs/PRIVACY.md), and [contributing](CONTRIBUTING.md). MIT applies to this project's original source, not to independently installed dependencies.

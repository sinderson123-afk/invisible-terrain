# 硬件、驱动与兼容性

## 验证范围

| 路径 | 状态 | 不应推断的结论 |
| --- | --- | --- |
| 原型：RTL-SDR Blog V4 / Windows | 曾实测接收、调频、FM 收听、搜台与重启 | 不代表公开版适配器已经实机回归 |
| 公开版 `rtl` 后端 | 软件测试；实机回归待补 | 不代表所有 RTL2832U 型号、调谐器或系统都兼容 |
| 公开版 `soapy` 后端 | 模拟驱动契约测试；实验性、无实机验证 | 不代表已验证 HackRF、Airspy、SDRplay、LimeSDR 或 USRP |
| 合成演示 | 不使用 SDR 硬件 | 不能用于判断天线、驱动和真实接收效果 |
| Wallpaper Engine | 2.7.0.3 在一台 Windows 机器有稳定记录；当前发行版回归待补 | 不保证所有版本、显卡和开机时序兼容 |

后端/浏览器跨平台测试与 Windows 桌面集成是不同层次。自动化测试不能代替设备、声卡和 Wallpaper Engine 实测。

## 原生 RTL 后端

需要支持该设备的 `librtlsdr`（Windows 常名为 `rtlsdr.dll`）及其依赖。请从设备厂商或上游项目获取与操作系统、Python 架构匹配的版本；本仓库不附带这些文件。

- [Osmocom rtl-sdr 上游](https://github.com/osmocom/rtl-sdr)
- [RTL-SDR Blog 驱动分支](https://github.com/rtlsdrblog/rtl-sdr-blog)
- [RTL-SDR Blog V4 官方使用指南](https://www.rtl-sdr.com/v4/)

Windows 的 USB 驱动安装请按设备官方指南进行，只修改已确认的 SDR 接口，不要批量替换其他 USB 设备驱动。Linux 的 USB 权限与驱动占用也应按所用发行版/上游说明处理，不必以管理员身份运行整个应用。

先关闭正在占用同一接收器的 SDR 软件，然后列出设备：

```sh
python app/original_bridge.py --backend rtl --list-devices
```

若原生库未被发现，显式指定路径，例如 Windows：

```sh
python app/original_bridge.py --backend rtl --rtl-library "C:/SDR-libraries/rtlsdr.dll" --list-devices
python app/original_bridge.py --backend rtl --rtl-library "C:/SDR-libraries/rtlsdr.dll" --device-index 0 --sample-rate 2400000 --open --keep-wallpaper
```

上述 DLL 路径是占位示例，不是仓库提供的文件。若有多台设备，使用当前枚举结果选择 `--device-index`；重插后索引可能变化。频率与采样率须同时符合实际设备和应用要求，输入范围并不保证硬件能覆盖。

`--frequency` 使用 Hz，可指定启动时的中心频率。接收后端、设备索引、采样率和通道等设置通过启动参数选择；当前控制台主要负责调谐、收听与电台目录，不提供完整的硬件设置界面。

## SoapySDR 后端（实验性）

SoapySDR 是接收接口层，不是随装随用的所有设备驱动集合。需要：

1. 与系统匹配的 SoapySDR 运行库。
2. 与当前解释器匹配的 Python 绑定；虚拟环境未必能看到系统安装的绑定。
3. 对应设备的 Soapy 模块、厂商库和 USB 权限/驱动。

按 [SoapySDR 安装说明](https://github.com/pothosware/SoapySDR/wiki) 和 [Python 绑定说明](https://github.com/pothosware/SoapySDR/wiki/PythonSupport) 配置。不要假定 `pip install SoapySDR` 就能完成原生运行库、绑定和设备模块的安装；`requirements.txt` 刻意不包含它。

```sh
python -c "import SoapySDR; print(SoapySDR.getAPIVersion())"
python app/original_bridge.py --backend soapy --list-devices
```

下面仅以 `driver=rtlsdr` 演示设备选择语法，**不是该组合已通过实机测试的承诺**：

```sh
python app/original_bridge.py --backend soapy --device-args "driver=rtlsdr" --channel 0 --sample-rate 2400000 --open --keep-wallpaper
```

设备参数应以实际枚举和厂商文档为准。`--gain DB` 指定接收增益，`--antenna NAME` 指定接收天线端口；只有确认设备支持时才传入。不同设备的增益、采样率、天线名称与 RX 通道数不同。本项目不调用发射功能，不自动打开偏置电源，也不修改固件。

## 收听与搜台限制

- 安装 `requirements-audio.txt` 后才具备完整音频依赖。部分系统还需单独安装 PortAudio，参照 [sounddevice 官方安装说明](https://python-sounddevice.readthedocs.io/en/0.5.6/installation.html)。
- 输出为 48 kHz 单声道宽带 FM，87.5–108 MHz，50 μs 去加重；不提供 AM、NFM、立体声或 RDS。
- FM DSP 接收 1.8–10 MS/s 范围内可表示为受限有理数重采样比的输入率；整数 kHz 采样率可用。1.8/2/2.048/2.5/3/6/10 MS/s 已用合成信号验证音调、幅度及分块连续性，不等于对应硬件已验证。程序不会把设备返回的真实采样率伪装为另一个值。
- 每次启动声音默认关闭；首次试听先降低音量。合成模式不播放声音。
- 建议优先选择设备支持的 2–3 MS/s。高采样率需要更多 USB 带宽和 CPU；DSP 合成测试通过不等于所有电脑都能实时收听。若声音反复缓冲，请降低设备采样率、关闭其他接收软件后再测试。
- 搜台根据相对频谱能量筛选候选，不查询外部电台库；弱台、安静节目、干扰和天线条件都会影响结果。
- 搜台与地形/声音共用同一接收器，扫描期间暂时中断当前节目，结束或取消后恢复原频率。停止接收则保持停止。

## 提交实机结果

请使用仓库的 Hardware compatibility report 问题模板，报告设备型号、系统、Python 位数/版本、后端与驱动版本、参数、可复现步骤、停止后能否由其他软件重新打开设备，以及实际测试了哪些功能。

“枚举成功”“能出频谱”“能听到广播”“搜台可恢复”“重启后正常”是不同级别，分别说明。仅测试过一个频率，不要写“全部频段正常”。分享日志前删除序列号、个人路径、收藏台名和其他个人信息。仅接收当地允许接收的信号；不要把录音或 IQ 数据当作默认错误报告附件。

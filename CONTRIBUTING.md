# 参与贡献

欢迎改进视觉、DSP、无障碍、文档和设备适配。提交前请说明问题、修改范围及验证方式；较大的行为或依赖变更建议先开 issue 讨论。

## 本地验证

在仓库根目录，使用独立 Python 环境和 Node.js 22：

```sh
python -m pip install -r requirements-audio.txt
python -m unittest discover -s app -p "test_*.py"
node app/test_original_model.cjs
node app/test_original_audio_ui.cjs
node app/test_original_stations_ui.cjs
```

测试应使用合成数据、模拟设备和临时目录，不依赖某个开发者的 SDR、声卡、Steam 安装位置或个人偏好文件。真实设备测试单独进行，必须明确记录型号与驱动版本；没有实机的后端只能标为“软件/模拟测试通过”。

## 需要保留的边界

- 仅接收，不增加隐式发射、偏置电源或固件写入。
- 每次启动声音默认关闭；不能把记忆音量变成自动播放。
- 真实接收中断不能静默伪装成合成电波；演示必须明确标注。
- 单一接收器所有权；调频、扫描、取消、停止和异常路径都要释放设备。
- 本机服务维持回环绑定、来源检查和控制令牌，不为调试开放公网。
- 依赖缺失应给出可理解的错误；不要自动下载驱动或修改系统 USB 设置。
- 不修改用户的其他壁纸、Steam/Wallpaper Engine 版本或全局系统设置。

新增行为请补回归测试。前端修改同时检查浏览器控制台与只读壁纸模式、暂停/恢复、空数据、服务断开和小屏布局。跨系统测试通过不等于 Wallpaper Engine 兼容性通过。

## 文件与授权

提交前检查 `git diff --cached`。不要提交 DLL/驱动、虚拟环境、收藏、个人配置、日志、会话令牌、录音、IQ、未脱敏截图或第三方壁纸资源。

只提交有权提供的代码和素材；本项目原创部分采用 [MIT](LICENSE)，贡献默认使用相同许可。新增第三方代码或可分发资源时，先核对许可证、注明来源并更新 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；不能仅凭“免费”假定可重分发。原生库的分发与组合许可须单独审查。

涉及硬件的反馈请使用 [Hardware compatibility report](https://github.com/sinderson123-afk/invisible-terrain/issues/new?template=hardware.yml) 模板，并按 [隐私说明](docs/PRIVACY.md) 脱敏。请让每个拉取请求聚焦一个可验证的改进。

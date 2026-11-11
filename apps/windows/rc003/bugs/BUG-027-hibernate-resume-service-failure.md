# BUG-027：休眠恢复后“遥控器服务”变红且必须手动重启

> 历史记录：本文的状态、命令、版本和待测项只针对当时批次，不是当前操作指引。
> 当前能力见 [使用说明](../README.md)，最新验收见 [测试记录](../TESTING.md)，后续处理见 [维护记录](../MAINTENANCE.md)。

状态：代码已修复，自动检查通过，待真机休眠复测。

记录日期：2026-11-11

## 现象

电脑休眠并唤醒后，设备页“遥控器服务”显示红色并提示“点击‘重启服务’”，
按键与语音均不可用。手动重启服务后立即恢复正常。较短的睡眠通常可以自行恢复，
真正的休眠（数十分钟以上）几乎必然失败。

## 证据

同一台电脑 `%LOCALAPPDATA%\RemoteMic\RC003\logs\app.log.1` 的完整记录：

- 08:32:56 至 09:28:32 之间没有任何日志，共 3336 秒，对应用户的休眠区间。
- 09:28:32.919 worker 报 `peer_lost`；09:28:42.089 `device_voice_close`
  记录 `reason=cleanup_failed`。
- 失败的操作是 `subscription_disable`，`outcome=timeout`、`status=1`
  （`GattCommunicationStatus.Unreachable`）。
- 09:42:48 升权接收进程以退出码 2 结束，父进程记录为异常退出。
- 直到 09:46:38 用户手动“重启服务”后才恢复。

同一文件内 08:28:42 至 08:32:46 仅 244 秒的中断则自动恢复成功
（`Chromecast retry offered` 与 `receiver session restarted`），说明短时中断
与休眠走的是同一条代码路径，差别只在中断长度。

另有一条独立证据：受限的 `pnp` 查询预算为 8000 毫秒，日志记录的
`duration_ms` 却是 3344109；`process_exit_wait` 的 `elapsed_ms=845969`
也与 846 秒的实际间隔一致。这说明计时源在休眠期间继续累加。

## 根本原因

### 计时源把休眠时间算作运行时间

Windows 上 `time.monotonic()` 由中断时间派生，休眠期间继续累加。
本机实测它与 `QueryUnbiasedInterruptTime()` 相差 4369 秒，即累计休眠时长。
接收进程全部使用 `time.monotonic()` 作为“存活预算”，因此唤醒瞬间所有期限
同时过期：

- `ParentLease` 的心跳窗口为 10 秒。worker 在读取管道之前先检查租约，
  唤醒后租约已过期数百倍，于是 worker 在父进程的心跳被读取之前就自行结束本次运行。
- 启动、采集就绪、报告新鲜度等其余期限同样在唤醒瞬间失效。

### 清理把“链路已断开”当作“资源未释放”

唤醒后蓝牙射频与 HID-over-GATT 宿主尚未重新接入，注销 CCCD 的写入返回
`Unreachable` 或超时。代码把该结果记为清理失败，于是：

- worker 判定 `cleanup_ok=False`，不发送 `retry` 事件，直接以退出码 2 结束；
- 父进程 `ChromecastRuntime` 按设计停在“保留可见原因，直到用户停止或重启”，
  而设置窗口对谷歌遥控器明确不执行自动恢复（该限制用于避免非预期的 UAC 提示）。

### 状态措辞与界面颜色依赖偶然分支

失效会话发布的是 `chromecast_peer_lost` 与 `hid_tap_state=failed`。
`FAILED_RAW_INPUT_STATES` 只列了 RC003 的值，`chromecast_*` 不在其中，
因此 `input_channels_failed()` 返回假，`bridgeRestartRecommended` 也为假。
设备页的红色并非来自状态契约，而是 QML 默认分支恰好落到 `error`；
5 秒心跳又让 `updated_at` 始终新鲜，时效检测同样不会触发。

结论：这是缺乏休眠恢复设计的结构性问题，不是设备或驱动故障。
仓库中原本没有任何电源管理处理（无 `WM_POWERBROADCAST`、
无 `RegisterPowerSuspendResumeNotification`、无电源设置 GUID）。

## 修复设计

### 1. 统一使用排除休眠的计时源

新增 `suspend_clock`，在 Windows 上以 `QueryUnbiasedInterruptTime`
为基础，非 Windows 或无法解析入口时回退到 `time.monotonic()`，不虚构未经验证的时钟。
接收链路（worker、client、device、HID tap、语音接收）的期限与时间戳统一改用该计时源。
两个进程读取同一基准，worker 生成的时间戳在唤醒后仍可与父进程的新鲜度窗口比较。

同时修正 `Pipe.connect()`：调用方传入的期限与循环内的时钟必须同源，
否则期限看起来早已过期并取消一次本来正常的连接。

### 2. 清理区分“已断开”与“未释放”

`close_voice()` 在注销 CCCD 失败时检查链路状态。若链路已不在连接态，
对应订阅不可能继续存在，因此不计为本次运行仍占用的资源；链路仍连接时
仍按原样报告失败。清理成功的判据本身没有放宽。

### 3. 有限且有界的恢复

- `input_interface_unavailable` 加入可恢复原因。唤醒后 Windows 重新暴露
  HID collection 的时刻晚于 worker 的启动期限，而设备仍然配对且已被选中，
  该原因在定义上是暂时的。
- 检测到唤醒后，把同样的重试阶梯（次数不变）分散到更长的间隔
  （5、15、30 秒），并在运行日志中记录一次“resume detected”。
- 设置窗口在确认已唤醒、且接收进程已终止时自动重启服务一次。仅当
  辅助进程已具备管理员权限时执行，避免把唤醒变成非预期的 UAC 提示；
  否则保留原有的“重启服务”按钮。

### 4. 状态措辞遵循契约

- 可恢复的中断发布为 `chromecast_recovering`，界面显示“正在恢复”，
  既不宣称永久失败，也不在此期间提供重启操作。
- 终止原因单独在运行真正结束时按真实原因发布。
- `CHROMECAST_TERMINAL_STATES` 明确列出不再重试的原因，
  `input_channels_failed()` 据此判定，不再依赖 QML 的默认分支。

## 验证

- 新增 `tests/test_suspend_clock.py`，覆盖计时源不超前于含休眠基准、
  租约在超过心跳窗口的休眠后仍有效、真正流逝的时间仍会使租约过期、
  唤醒检测每次休眠只报告一次、恢复间隔有界且不短于原间隔，
  以及可恢复与终止原因的措辞区分。
- `tests/test_chromecast*.py` 全部 529 项通过；唯一例外
  `test_wrong_listener_pid_is_rejected_before_frida_connection`
  依赖本机 TCP 所有者表，在未修改的基线上同样不稳定。
- 既有用例改为通过 `suspend_clock` 注入时钟，与实际运行的代码同源。

## 待实测

- 真实休眠唤醒后的自动恢复，包括语音通道与输入法上屏。
- 长时间休眠（数小时）与休眠后立即按键。
- 未启用管理员辅助进程时，唤醒后仍应只提示手动重启而不弹 UAC。

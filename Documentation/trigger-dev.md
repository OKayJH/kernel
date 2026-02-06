# trigger-dev（GPIO按键触发拍照）说明

本模块用于把一个**通用 GPIO 输入**（例如按键）做成一个输入设备 `trigger-dev`：  
当 GPIO 检测到**高电平（按下）**时：

- 终端（`dmesg`）打印**红色** `down`
- **仅触发一次**动作（一直按住不会重复触发）
- 执行一次与下面命令**完全等价**的动作（内核里写同一个 sysfs 节点）：

```bash
echo 1 > /sys/bus/i2c/devices/1-0036/trigger
```

当 GPIO 变为**低电平（松开）**时：

- 终端打印 `up`
- 只有松开后再次按下，才会触发下一次

---

## 原理（一句话版）

`trigger-dev` 通过 GPIO 中断 + 去抖，在“按下沿”确认后：

- 上报一个 input 按键事件（默认 `KEY_CAMERA`）
- 在 workqueue 里向你配置的 `trigger-path` 写入 `"1\n"`，从而触发目标设备的 sysfs `store()`（效果等价于用户态 `echo`）

---

## 代码位置

- 驱动：`drivers/input/misc/trigger-dev.c`
- Kconfig：`drivers/input/misc/Kconfig`（`CONFIG_INPUT_TRIGGER_DEV`）
- Makefile：`drivers/input/misc/Makefile`
- RK3588 overlay：
  - `arch/arm64/boot/dts/rockchip/overlay/rk3588-lubancat-5-trigger-dev-overlay.dts`
  - `arch/arm64/boot/dts/rockchip/overlay/rk3588-lubancat-5io-trigger-dev-overlay.dts`

---

## 设备树可改的地方（最常用）

`trigger-dev` 通过设备树配置，常用字段如下：

- **`input-gpios`**：输入 GPIO（默认 `GPIO1_A3`）
  - 兼容教程写法也支持 **`button-gpios`**（驱动会自动尝试 `input-gpios` / `button-gpios`）
- **`debounce-ms`**：去抖时间（ms），默认 20
- **`linux,code`**：上报到 input 子系统的按键码，默认 `KEY_CAMERA`
- **`trigger-path`**：需要写入的 sysfs 文件路径（最关键）
- **`trigger-value`**：写入内容（默认 `"1"`；驱动会自动追加换行）

也支持不写 `trigger-path`，改用下面三项自动拼接路径：

- `trigger-i2c-bus`（例如 5）
- `trigger-i2c-addr`（例如 0x36）
- `trigger-attr`（默认 `"trigger"`）

最终会写入：

`/sys/bus/i2c/devices/<bus>-<addr4位十六进制>/<attr>`

---

## 触发“偶尔预览不变/像没拍到”的原因与修复

现象：按键 `down/up` 日志每次都有，但预览画面**偶尔**看起来没有刷新一帧。

常见原因不是按键驱动，而是 **相机处于触发模式时的“等待一帧”时间不够**：在自动曝光/低光场景下，ISP/驱动可能通过增大 `VBlank/VTS` 来降低帧率以获得更长曝光。如果仍按“30fps 的固定 33ms”去等待，就可能在帧还没输出完时切回 standby，导致这一触发没有产生可见新帧。

本次改动已在 `os08a20` 驱动中修复：触发等待时间会按当前 `VBlank/VTS` **动态缩放**，并支持 DT 手动调参（见下节）。

---

## 可手动调整的时间参数（建议从 DT/overlay 调）

### `trigger-dev`（按键侧）

- **`debounce-ms`**：按键去抖（ms）。想更快响应可从 20 降到 5~10（过小可能抖动误触发）。

### `os08a20`（相机触发侧，写在相机节点 DT/overlay）

以下属性写在 `ovti,os08a20` 节点（也就是你已有的 `cam*-os08a20-*-overlay.dts` 里）：

- **`rockchip,fsin-pulse-us`**：FSIN 脉冲宽度（us，默认 50）。硬件边沿不稳/线长时可适当增大（例如 100~500）。
- **`rockchip,fsin-settle-us`**：在拉起 streaming 并写完曝光增益后，到打 FSIN 脉冲前的等待（us，默认 200）。极端情况下可加大提高稳定性（例如 1000~5000）。
- **`rockchip,trigger-frame-margin-us`**：FSIN 脉冲后额外等待（us，默认 0）。如果你仍然偶发“预览不变”，可先尝试加 5000~20000。
- **`rockchip,trigger-frame-wait-us`**：强制覆盖“等待一帧”的时间（us，默认 0=自动计算）。调试时可以直接设成 100000（100ms）来验证是否是“等待不够”导致的问题。

调参建议（优先顺序）：

1. 先用默认（自动按 VBlank 缩放）观察是否还会偶发不刷新  
2. 仍偶发：加 `trigger-frame-margin-us`（5ms → 20ms）  
3. 还不行：临时设 `trigger-frame-wait-us = <100000>` 验证根因  
4. 最后再按实际场景把数值收敛到最小稳定值（兼顾速度）

---

## 每个关键函数做什么

文件：`drivers/input/misc/trigger-dev.c`

- **`trigger_dev_irq()`**：GPIO 中断处理函数，只负责调度 `debounce_work`（不做耗时操作）
- **`trigger_dev_debounce_work()`**：去抖后读取 GPIO 当前电平，交给 `trigger_dev_handle_state()`
- **`trigger_dev_handle_state()`**：
  - 电平变高：打印红色 `down`、上报按键按下事件、把触发次数 `+1` 并调度 `trigger_work`
  - 电平变低：打印 `up`、上报按键松开事件
  - 通过 `tdev->pressed` 保证“一次按下只触发一次”
- **`trigger_dev_trigger_work()`**：在 workqueue 里执行真正的触发动作；用 `trigger_pending` 计数保证快速多次按下也不会丢触发
- **`trigger_dev_write_once()`**：用 `filp_open()` + `kernel_write()` 向 `trigger-path` 写入 payload（等价 `echo`）
- **`trigger_dev_parse_dt()`**：解析设备树参数（GPIO、去抖、keycode、trigger-path/value）
- **`trigger_dev_probe()`**：初始化 GPIO、input 设备、申请中断、初始化 work
- **`trigger_dev_remove()`**：模块卸载/设备解绑时取消 work，避免 UAF

---

## 使用/验证（推荐步骤）

1. 在 `uEnv.txt` 中启用对应 overlay（取消注释 `dtoverlay=...trigger-dev...dtbo`）
   - 注意：`dtbo` 必须放在**启动分区**的 `/dtb/overlay/` 目录下（U-Boot 读取不到会导致 overlay 加载失败，甚至崩溃）
2. 重启后确认模块已加载（若系统未自动加载，可手动）：

```bash
modprobe trigger-dev
```

3. 观察日志并按键测试：

```bash
dmesg -w
```

看到红色 `down` / `up`，并在按下时打印一次“摄像头触发一次拍照”。

---

## 移植到其他平台/其他分支怎么做

只要满足“有一个 GPIO 输入 + 有一个可写 sysfs 触发点”，就能移植：

1. 拷贝驱动文件：`drivers/input/misc/trigger-dev.c`
2. 拷贝 Kconfig/Makefile 增量：
   - `drivers/input/misc/Kconfig` 增加 `CONFIG_INPUT_TRIGGER_DEV`
   - `drivers/input/misc/Makefile` 增加 `trigger-dev.o`
3. 在你的 defconfig 里打开（建议模块）：
   - `CONFIG_INPUT_TRIGGER_DEV=m`
4. 复制并修改 overlay：
   - 换成你实际的 GPIO（`input-gpios` / `rockchip,pins`）
   - 改成你实际的触发 sysfs（`trigger-path`）
5. 重新编译并部署 `*.dtbo` 和 `trigger-dev.ko`，在 `uEnv.txt` 里选择是否启用 overlay

---

## 常见踩坑

- **一直按住也只触发一次**是设计行为：必须先松开再按下才会触发下一次。
- **GPIO 引脚冲突/被占用**会导致“按键毫无反应”：
  - 例：LubanCat-5IO 默认 DTS 中 `GPIO0_PD3 (RK_PD3)` 被 Type-C 控制器 `fusb302` 用作中断脚（不要再复用做按键）
  - 排查：在板子上执行 `cat /sys/kernel/debug/gpio` 看该 GPIO 是否已被其他设备占用
- 如果写 sysfs 失败（`-ENOENT/-EBUSY` 等），通常是：
  - `trigger-path` 不存在（目标设备/驱动没起来）
  - 相机未处于可触发状态（例如未 streaming，或未启用对应 trigger 模式）



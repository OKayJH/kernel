# OS08A20 小工具（曝光/增益持久化 & 一键白平衡）

本目录提供两个用户态脚本，方便你在终端/程序里：

- 固定曝光/增益，并把参数保存到配置文件，**系统重启后自动恢复**
- 用白纸做参考，一键计算白平衡（固定 G，调 R/B 让 RGB 均值相等）

## 1) 曝光/增益保存与开机自动恢复

脚本：`os08a20_camctl.py`

### 保存（立刻生效 + 落盘）

```sh
sudo python3 tools/os08a20/os08a20_camctl.py set \
  --exposure 2224 --analogue-gain 1024 \
  --save
```

默认会写入：`/etc/os08a20.json`

### 手动应用（不落盘）

```sh
sudo python3 tools/os08a20/os08a20_camctl.py apply
```

> `apply` 默认从 `/etc/os08a20.json` 读取配置并写入 V4L2 控件。
>
> 配置文件可包含（按驱动是否支持而定）：
>
> - `exposure`
> - `analogue_gain`
> - `white_balance_automatic`
> - `red_balance`
> - `blue_balance`

### 只设置白平衡（可选）

```sh
sudo python3 tools/os08a20/os08a20_camctl.py set-wb --white-balance-automatic 0 --red-balance 1089 --blue-balance 924 --save
```

### 配置 systemd 开机自动应用

1) 复制脚本到固定路径（推荐）：

```sh
sudo install -Dm755 tools/os08a20/os08a20_camctl.py /usr/local/bin/os08a20_camctl.py
sudo install -Dm644 tools/os08a20/os08a20-apply.service /etc/systemd/system/os08a20-apply.service
```

2) 启用服务：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now os08a20-apply.service
```

> 注意：服务会在 **驱动存在且设备节点可访问** 时把配置写入 V4L2 控件；如果当时传感器未上电也没关系，控件值会被缓存，后续 `STREAMON` 时驱动会自动下发到寄存器。

## 2) 一键白平衡（白纸，固定 G，算 R/B）

脚本：`os08a20_awb.py`

建议先抓一张白纸图片（PPM 最省依赖）：

```sh
ffmpeg -f video4linux2 -i /dev/video0 -vframes 1 frame.ppm
python3 tools/os08a20/os08a20_awb.py --image frame.ppm
```

如果你的设备节点支持 `red_balance` / `blue_balance` 控件，可以直接 apply：

```sh
sudo python3 tools/os08a20/os08a20_awb.py --image frame.ppm --apply-dev /dev/video0
```



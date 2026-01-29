### LubanCat uEnv文件说明

在U-boot启动时，通过SDRADC检测硬件ID引脚，并根据硬件ID值加载相应的uEnv.txt到U-boot环境变量中，

以此来实现不同板卡通用一个固件。

#### 支持板卡：

- LubanCat Zero 系列 基于RK3566
- LubanCat1 系列 基于RK3566
- LubanCat2 系列 基于RK3568
- LubanCat4 系列 基于RK3588s

#### uEnv文件命名规范

- 使用uEnv开头
- 使用.txt结尾
- 中间为板卡名称


#### 注意事项

- **切勿在Uboot终端中使用saveenv命令，这会覆盖uEnv.txt文件，导致系统无法启动，需要重新烧录固件。**

- RK3566固件可通用全系列，RK3568固件仅适用于LubanCat2系列。
- 如未读取到正确的ID，将加载uEnv.txt运行启动系统的最小外设。
- LubanCat2C/1C为核心板，其16进制板卡ID前两位为核心板ID，后两位为底板ID。由于核心板无法单独运行，需配套底板使用，加载的环境变量文件也是底板的对应文件。
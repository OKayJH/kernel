#!/usr/bin/env python3

import argparse
import subprocess
import sys
from pathlib import Path


def read_ppm_p6(path):
    file_path = Path(path)
    data = file_path.read_bytes()

    index = 0
    if not data.startswith(b"P6"):
        raise ValueError("只支持 PPM P6 格式（建议用 ffmpeg 导出 .ppm）")

    def read_token():
        nonlocal index
        while index < len(data) and data[index] in b" \t\r\n":
            index += 1
        if index < len(data) and data[index] == ord("#"):
            while index < len(data) and data[index] != ord("\n"):
                index += 1
            return read_token()
        start = index
        while index < len(data) and data[index] not in b" \t\r\n":
            index += 1
        return data[start:index]

    magic = read_token()
    if magic != b"P6":
        raise ValueError("PPM 头解析失败")

    width = int(read_token())
    height = int(read_token())
    maxval = int(read_token())
    if maxval <= 0 or maxval > 255:
        raise ValueError(f"不支持的 maxval={maxval}（请导出 8-bit PPM）")

    while index < len(data) and data[index] in b" \t\r\n":
        index += 1

    pixel_bytes = width * height * 3
    pixel_data = data[index : index + pixel_bytes]
    if len(pixel_data) != pixel_bytes:
        raise ValueError("PPM 像素数据长度不匹配")

    return width, height, pixel_data


def compute_rgb_means(width, height, pixel_data, step):
    view = memoryview(pixel_data)
    sum_r = 0
    sum_g = 0
    sum_b = 0
    count = 0

    step = max(1, int(step))
    for y in range(0, height, step):
        row_base = y * width * 3
        for x in range(0, width, step):
            index = row_base + x * 3
            sum_r += view[index]
            sum_g += view[index + 1]
            sum_b += view[index + 2]
            count += 1

    if count == 0:
        raise ValueError("采样点为 0，请检查 step 参数")

    r_mean = sum_r / count
    g_mean = sum_g / count
    b_mean = sum_b / count
    return r_mean, g_mean, b_mean


def calc_awb_gains(r_mean, g_mean, b_mean, g_gain):
    if r_mean <= 0.5 or b_mean <= 0.5:
        raise ValueError("R/B 均值过小，疑似全黑或通道数据异常")

    r_gain = g_gain * (g_mean / r_mean)
    b_gain = g_gain * (g_mean / b_mean)
    return int(round(r_gain)), int(round(g_gain)), int(round(b_gain))


def run_v4l2_set(dev, red_ctrl, blue_ctrl, r_gain, b_gain):
    cmd = [
        "v4l2-ctl",
        "-d",
        dev,
        f"--set-ctrl={red_ctrl}={int(r_gain)},{blue_ctrl}={int(b_gain)}",
    ]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"设置白平衡控件失败: {' '.join(cmd)}\n"
            f"stderr:\n{result.stderr}"
        )


def main():
    parser = argparse.ArgumentParser(description="白纸一键白平衡：固定 G，计算并输出/应用 R/B 增益（RGB 均值相等）")
    parser.add_argument("--image", required=True, help="输入图片（PPM P6, 8-bit）")
    parser.add_argument("--step", type=int, default=8, help="采样步长，越大越快（默认 8）")
    parser.add_argument("--g-gain", type=int, default=1024, help="固定 G 增益（默认 1024）")
    parser.add_argument("--apply-dev", default="", help="可选：直接对该 V4L2 设备节点应用增益，例如 /dev/video0")
    parser.add_argument(
        "--auto-wb-ctrl",
        default="white_balance_automatic",
        help="自动白平衡开关控件名（默认 white_balance_automatic），应用增益前会尝试先置 0(手动)",
    )
    parser.add_argument("--red-ctrl", default="red_balance", help="红通道增益控件名，默认 red_balance")
    parser.add_argument("--blue-ctrl", default="blue_balance", help="蓝通道增益控件名，默认 blue_balance")
    args = parser.parse_args()

    width, height, pixel_data = read_ppm_p6(args.image)
    r_mean, g_mean, b_mean = compute_rgb_means(width, height, pixel_data, args.step)
    r_gain, g_gain, b_gain = calc_awb_gains(r_mean, g_mean, b_mean, args.g_gain)

    print(f"图像: {width}x{height}, step={args.step}")
    print(f"RGB 均值: R={r_mean:.3f}, G={g_mean:.3f}, B={b_mean:.3f}")
    print(f"建议增益(固定G): R={r_gain}, G={g_gain}, B={b_gain}")

    if args.apply_dev:
        if args.auto_wb_ctrl:
            subprocess.run(
                ["v4l2-ctl", "-d", args.apply_dev, f"--set-ctrl={args.auto_wb_ctrl}=0"],
                text=True,
                capture_output=True,
            )
        run_v4l2_set(args.apply_dev, args.red_ctrl, args.blue_ctrl, r_gain, b_gain)
        print(f"已应用到 {args.apply_dev}: {args.red_ctrl}={r_gain}, {args.blue_ctrl}={b_gain}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)



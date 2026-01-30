#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd, check=True):
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"命令失败: {' '.join(cmd)}\n"
            f"exit={result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n"
        )
    return result


def find_os08a20_subdev(name_keyword="os08a20"):
    sys_class = Path("/sys/class/video4linux")
    if not sys_class.exists():
        return None

    keyword = name_keyword.strip().lower()
    for entry in sorted(sys_class.glob("v4l-subdev*")):
        name_path = entry / "name"
        try:
            dev_name = name_path.read_text().strip()
        except OSError:
            continue

        if keyword in dev_name.lower():
            return f"/dev/{entry.name}"
    return None


def load_config(config_path):
    text = Path(config_path).read_text(encoding="utf-8")
    data = json.loads(text)
    config = {}
    for key in ["exposure", "analogue_gain", "white_balance_automatic", "red_balance", "blue_balance"]:
        if key in data:
            config[key] = int(data[key])
    return config


def save_config(config_path, config):
    data = {k: int(v) for k, v in config.items()}
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def set_exposure_gain(subdev, exposure, anal_gain):
    run_cmd(["v4l2-ctl", "-d", subdev, f"--set-ctrl=exposure={int(exposure)},analogue_gain={int(anal_gain)}"])


def set_white_balance(subdev, auto_wb, red_balance, blue_balance):
    if auto_wb is not None:
        run_cmd(["v4l2-ctl", "-d", subdev, f"--set-ctrl=white_balance_automatic={int(auto_wb)}"], check=False)

    if red_balance is None and blue_balance is None:
        return

    # Best effort: for manual gains, force auto wb off first.
    run_cmd(["v4l2-ctl", "-d", subdev, "--set-ctrl=white_balance_automatic=0"], check=False)

    parts = []
    if red_balance is not None:
        parts.append(f"red_balance={int(red_balance)}")
    if blue_balance is not None:
        parts.append(f"blue_balance={int(blue_balance)}")
    run_cmd(["v4l2-ctl", "-d", subdev, f"--set-ctrl={','.join(parts)}"])


def get_one_ctrl(subdev, ctrl_name):
    result = run_cmd(["v4l2-ctl", "-d", subdev, f"--get-ctrl={ctrl_name}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_ctrls(subdev):
    lines = []
    for ctrl_name in ["exposure", "analogue_gain", "white_balance_automatic", "red_balance", "blue_balance"]:
        line = get_one_ctrl(subdev, ctrl_name)
        if line:
            lines.append(line)
    return "\n".join(lines)


def apply_config(subdev, config):
    if "exposure" in config and "analogue_gain" in config:
        set_exposure_gain(subdev, config["exposure"], config["analogue_gain"])

    set_white_balance(
        subdev,
        config.get("white_balance_automatic"),
        config.get("red_balance"),
        config.get("blue_balance"),
    )


def main():
    parser = argparse.ArgumentParser(description="OS08A20 曝光/增益/白平衡 设置与持久化")
    parser.add_argument(
        "--subdev",
        default="auto",
        help="传感器 subdev 节点，例如 /dev/v4l-subdev2；默认 auto 自动查找",
    )
    parser.add_argument(
        "--config",
        default="/etc/os08a20.json",
        help="配置文件路径，默认 /etc/os08a20.json",
    )

    subparsers = parser.add_subparsers(dest="cmd", required=True)

    subparsers.add_parser("find", help="打印自动检测到的 /dev/v4l-subdevX")

    subparsers.add_parser("get", help="读取当前 exposure / analogue_gain / (可选) white balance")

    set_parser = subparsers.add_parser("set", help="设置 exposure / analogue_gain")
    set_parser.add_argument("--exposure", type=int, required=True, help="V4L2_CID_EXPOSURE")
    set_parser.add_argument("--analogue-gain", type=int, required=True, help="V4L2_CID_ANALOGUE_GAIN")
    set_parser.add_argument("--white-balance-automatic", type=int, choices=[0, 1], help="V4L2_CID_AUTO_WHITE_BALANCE")
    set_parser.add_argument("--red-balance", type=int, help="V4L2_CID_RED_BALANCE")
    set_parser.add_argument("--blue-balance", type=int, help="V4L2_CID_BLUE_BALANCE")
    set_parser.add_argument("--save", action="store_true", help="同时保存到配置文件")

    set_wb_parser = subparsers.add_parser("set-wb", help="只设置白平衡（auto / red / blue）")
    set_wb_parser.add_argument("--white-balance-automatic", type=int, choices=[0, 1], help="V4L2_CID_AUTO_WHITE_BALANCE")
    set_wb_parser.add_argument("--red-balance", type=int, help="V4L2_CID_RED_BALANCE")
    set_wb_parser.add_argument("--blue-balance", type=int, help="V4L2_CID_BLUE_BALANCE")
    set_wb_parser.add_argument("--save", action="store_true", help="同时保存到配置文件")

    apply_parser = subparsers.add_parser("apply", help="从配置文件读取并应用")
    apply_parser.add_argument("--save", action="store_true", help="重新写回配置文件（规范化格式）")

    args = parser.parse_args()

    subdev = args.subdev
    if subdev == "auto":
        subdev = find_os08a20_subdev()
        if not subdev:
            raise RuntimeError("未找到 os08a20 的 subdev，请用 --subdev 指定，例如 /dev/v4l-subdev2")

    if not os.path.exists(subdev):
        raise RuntimeError(f"设备节点不存在: {subdev}")

    if args.cmd == "find":
        print(subdev)
        return 0

    if args.cmd == "get":
        print(get_ctrls(subdev))
        return 0

    if args.cmd == "set":
        apply_config(
            subdev,
            {
                "exposure": args.exposure,
                "analogue_gain": args.analogue_gain,
                **(
                    {"white_balance_automatic": args.white_balance_automatic}
                    if args.white_balance_automatic is not None
                    else {}
                ),
                **({"red_balance": args.red_balance} if args.red_balance is not None else {}),
                **({"blue_balance": args.blue_balance} if args.blue_balance is not None else {}),
            },
        )
        if args.save:
            config = load_config(args.config) if os.path.exists(args.config) else {}
            config["exposure"] = args.exposure
            config["analogue_gain"] = args.analogue_gain
            if args.white_balance_automatic is not None:
                config["white_balance_automatic"] = args.white_balance_automatic
            if args.red_balance is not None:
                config["red_balance"] = args.red_balance
            if args.blue_balance is not None:
                config["blue_balance"] = args.blue_balance
            save_config(args.config, config)
            print(f"已保存: {args.config}")
        print(get_ctrls(subdev))
        return 0

    if args.cmd == "set-wb":
        set_white_balance(subdev, args.white_balance_automatic, args.red_balance, args.blue_balance)
        if args.save:
            config = load_config(args.config) if os.path.exists(args.config) else {}
            if args.white_balance_automatic is not None:
                config["white_balance_automatic"] = args.white_balance_automatic
            if args.red_balance is not None:
                config["red_balance"] = args.red_balance
            if args.blue_balance is not None:
                config["blue_balance"] = args.blue_balance
            save_config(args.config, config)
            print(f"已保存: {args.config}")
        print(get_ctrls(subdev))
        return 0

    if args.cmd == "apply":
        config = load_config(args.config)
        apply_config(subdev, config)
        if args.save:
            save_config(args.config, config)
        print(get_ctrls(subdev))
        return 0

    raise RuntimeError(f"未知命令: {args.cmd}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)



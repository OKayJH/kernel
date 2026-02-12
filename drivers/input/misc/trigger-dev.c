// SPDX-License-Identifier: GPL-2.0
/*
 * trigger-dev: Generic GPIO input device that triggers a sysfs "echo 1 > ..."
 *
 * Features:
 * - GPIO input (edge interrupt)
 * - Press/release prints: "down"(red) / "up"
 * - One-shot trigger per press (must release before next trigger)
 * - Writes a configurable sysfs path to trigger e.g. camera single-frame capture
 *
 * This driver is intended to be portable across Rockchip SoCs (RK3576/RK3588...)
 * and other platforms as long as a GPIO and a writable sysfs node are provided.
 */

#include <linux/atomic.h>
#include <linux/err.h>
#include <linux/fs.h>
#include <linux/gpio/consumer.h>
#include <linux/input.h>
#include <linux/interrupt.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/property.h>
#include <linux/slab.h>
#include <linux/workqueue.h>

#define TRIGGER_DEV_MODNAME "trigger-dev"

#define ANSI_RED   "\033[1;31m"
#define ANSI_RESET "\033[0m"

struct trigger_dev {
	struct device		*dev;
	struct gpio_desc	*input_gpiod;
	int			irq;

	/* Input device */
	struct input_dev	*input;
	u32			key_code;
	bool			pressed;

	/* Debounce (ms). If 0, handle state immediately. */
	u32			debounce_ms;
	struct delayed_work	debounce_work;

	/* Trigger action: write payload to sysfs path */
	char			*trigger_path;
	char			*trigger_payload;
	size_t			trigger_payload_len;

	atomic_t		trigger_pending;
	struct work_struct	trigger_work;
};

static int trigger_dev_write_once(struct trigger_dev *tdev)
{
	struct file *filp;
	loff_t pos = 0;
	ssize_t ret;

	if (!tdev->trigger_path || !tdev->trigger_payload)
		return -EINVAL;

	filp = filp_open(tdev->trigger_path, O_WRONLY, 0);
	if (IS_ERR(filp))
		return PTR_ERR(filp);

	ret = kernel_write(filp, tdev->trigger_payload,
			   tdev->trigger_payload_len, &pos);
	filp_close(filp, NULL);

	if (ret < 0)
		return (int)ret;
	if (ret != tdev->trigger_payload_len)
		return -EIO;

	return 0;
}

static void trigger_dev_trigger_work(struct work_struct *work)
{
	struct trigger_dev *tdev =
		container_of(work, struct trigger_dev, trigger_work);
	int ret;

	/* Drain pending triggers (presses) */
	while (atomic_dec_if_positive(&tdev->trigger_pending) >= 0) {
		ret = trigger_dev_write_once(tdev);
		if (ret) {
			dev_err(tdev->dev, "trigger write failed: path=%s ret=%d\n",
				tdev->trigger_path ? tdev->trigger_path : "<null>",
				ret);
		} else {
			dev_info(tdev->dev, "摄像头触发一次拍照\n");
		}
	}
}

static void trigger_dev_handle_state(struct trigger_dev *tdev, bool pressed_now)
{
	if (pressed_now == tdev->pressed)
		return;

	tdev->pressed = pressed_now;

	if (pressed_now) {
		dev_info(tdev->dev, ANSI_RED "down" ANSI_RESET "\n");
		input_report_key(tdev->input, tdev->key_code, 1);
		input_sync(tdev->input);

		/*
		 * Only one trigger per press. Pending counter ensures we don't
		 * lose fast repeated press cycles even if trigger_work is busy.
		 */
		atomic_inc(&tdev->trigger_pending);
		schedule_work(&tdev->trigger_work);
	} else {
		dev_info(tdev->dev, "up\n");
		input_report_key(tdev->input, tdev->key_code, 0);
		input_sync(tdev->input);
	}
}

static void trigger_dev_debounce_work(struct work_struct *work)
{
	struct trigger_dev *tdev =
		container_of(to_delayed_work(work), struct trigger_dev, debounce_work);
	int val;

	val = gpiod_get_value_cansleep(tdev->input_gpiod);
	if (val < 0) {
		dev_err(tdev->dev, "failed to read input gpio: %d\n", val);
		return;
	}

	trigger_dev_handle_state(tdev, !!val);
}

static irqreturn_t trigger_dev_irq(int irq, void *dev_id)
{
	struct trigger_dev *tdev = dev_id;

	mod_delayed_work(system_wq, &tdev->debounce_work,
			 msecs_to_jiffies(tdev->debounce_ms));
	return IRQ_HANDLED;
}

static int trigger_dev_parse_dt(struct device *dev, struct trigger_dev *tdev)
{
	const char *path;
	const char *value;
	const char *attr;
	u32 bus, addr;
	int ret;

	/* Optional: input key code (defaults to KEY_CAMERA) */
	tdev->key_code = KEY_CAMERA;
	device_property_read_u32(dev, "linux,code", &tdev->key_code);

	/* Debounce (ms). Default 20ms. */
	tdev->debounce_ms = 20;
	device_property_read_u32(dev, "debounce-ms", &tdev->debounce_ms);

	/*
	 * Trigger path:
	 * - preferred: trigger-path = "/sys/..."
	 * - fallback: trigger-i2c-bus + trigger-i2c-addr (+ optional trigger-attr)
	 */
	ret = device_property_read_string(dev, "trigger-path", &path);
	if (!ret) {
		tdev->trigger_path = devm_kstrdup(dev, path, GFP_KERNEL);
		if (!tdev->trigger_path)
			return -ENOMEM;
	} else {
		if (device_property_read_u32(dev, "trigger-i2c-bus", &bus) ||
		    device_property_read_u32(dev, "trigger-i2c-addr", &addr))
			return -EINVAL;

		attr = "trigger";
		device_property_read_string(dev, "trigger-attr", &attr);

		tdev->trigger_path = devm_kasprintf(dev, GFP_KERNEL,
						    "/sys/bus/i2c/devices/%u-%04x/%s",
						    bus, addr, attr);
		if (!tdev->trigger_path)
			return -ENOMEM;
	}

	/* Trigger payload. Default "1\n" */
	value = "1";
	device_property_read_string(dev, "trigger-value", &value);

	tdev->trigger_payload = devm_kasprintf(dev, GFP_KERNEL, "%s\n", value);
	if (!tdev->trigger_payload)
		return -ENOMEM;
	tdev->trigger_payload_len = strlen(tdev->trigger_payload);

	/* Try hardware debounce if available, else keep software debounce. */
	if (tdev->debounce_ms) {
		ret = gpiod_set_debounce(tdev->input_gpiod, tdev->debounce_ms * 1000);
		if (!ret)
			tdev->debounce_ms = 0;
	}

	return 0;
}

static int trigger_dev_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct trigger_dev *tdev;
	int irq;
	int ret;
	int val;
	int gpio;
	bool active_low;
	int gpiod_err;

	tdev = devm_kzalloc(dev, sizeof(*tdev), GFP_KERNEL);
	if (!tdev)
		return -ENOMEM;

	tdev->dev = dev;
	platform_set_drvdata(pdev, tdev);

	/*
	 * Primary DT property: input-gpios (con_id = "input")
	 * Compatibility with common tutorials: button-gpios (con_id = "button")
	 */
	tdev->input_gpiod = devm_gpiod_get(dev, "input", GPIOD_IN);
	if (IS_ERR(tdev->input_gpiod)) {
		gpiod_err = PTR_ERR(tdev->input_gpiod);
		if (gpiod_err == -ENOENT)
			tdev->input_gpiod = devm_gpiod_get(dev, "button", GPIOD_IN);
	}
	if (IS_ERR(tdev->input_gpiod)) {
		gpiod_err = PTR_ERR(tdev->input_gpiod);
		if (gpiod_err == -EBUSY)
			dev_err(dev, "input gpio is busy (already in use). Please choose a free GPIO in DT.\n");
		return dev_err_probe(dev, gpiod_err, "failed to get input gpio (input-gpios/button-gpios)\n");
	}

	ret = trigger_dev_parse_dt(dev, tdev);
	if (ret)
		return dev_err_probe(dev, ret, "invalid DT properties\n");

	tdev->input = devm_input_allocate_device(dev);
	if (!tdev->input)
		return -ENOMEM;

	tdev->input->name = TRIGGER_DEV_MODNAME;
	tdev->input->id.bustype = BUS_HOST;

	input_set_capability(tdev->input, EV_KEY, tdev->key_code);
	input_set_drvdata(tdev->input, tdev);

	INIT_DELAYED_WORK(&tdev->debounce_work, trigger_dev_debounce_work);
	INIT_WORK(&tdev->trigger_work, trigger_dev_trigger_work);
	atomic_set(&tdev->trigger_pending, 0);

	/* Initialize pressed state without triggering. */
	val = gpiod_get_value_cansleep(tdev->input_gpiod);
	if (val < 0)
		return dev_err_probe(dev, val, "failed to read initial gpio state\n");
	tdev->pressed = !!val;

	ret = input_register_device(tdev->input);
	if (ret)
		return dev_err_probe(dev, ret, "failed to register input device\n");

	/*
	 * If DT provides interrupts (platform IRQ), prefer it. Otherwise fall
	 * back to GPIO-to-IRQ mapping.
	 */
	irq = platform_get_irq_optional(pdev, 0);
	if (irq < 0)
		irq = gpiod_to_irq(tdev->input_gpiod);
	if (irq < 0)
		return dev_err_probe(dev, irq, "failed to get irq for input gpio\n");
	tdev->irq = irq;

	ret = devm_request_any_context_irq(dev, tdev->irq, trigger_dev_irq,
					   IRQF_TRIGGER_RISING | IRQF_TRIGGER_FALLING,
					   TRIGGER_DEV_MODNAME, tdev);
	if (ret)
		return dev_err_probe(dev, ret, "failed to request irq\n");

	gpio = desc_to_gpio(tdev->input_gpiod);
	active_low = gpiod_is_active_low(tdev->input_gpiod);
	dev_info(dev, "ready: gpio=%d irq=%d active_low=%d debounce_ms=%u key_code=%u trigger=%s payload=%s\n",
		 gpio,
		 tdev->irq,
		 active_low,
		 tdev->debounce_ms,
		 tdev->key_code,
		 tdev->trigger_path,
		 tdev->trigger_payload);

	return 0;
}

static int trigger_dev_remove(struct platform_device *pdev)
{
	struct trigger_dev *tdev = platform_get_drvdata(pdev);

	if (tdev) {
		cancel_delayed_work_sync(&tdev->debounce_work);
		cancel_work_sync(&tdev->trigger_work);
	}

	return 0;
}

static const struct of_device_id trigger_dev_of_match[] = {
	{ .compatible = "embedfire,trigger-dev" },
	{ }
};
MODULE_DEVICE_TABLE(of, trigger_dev_of_match);

static struct platform_driver trigger_dev_driver = {
	.probe  = trigger_dev_probe,
	.remove = trigger_dev_remove,
	.driver = {
		.name           = TRIGGER_DEV_MODNAME,
		.of_match_table = of_match_ptr(trigger_dev_of_match),
	},
};
module_platform_driver(trigger_dev_driver);

MODULE_DESCRIPTION("Generic GPIO input trigger device (sysfs write on press)");
MODULE_LICENSE("GPL");
MODULE_IMPORT_NS(VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver);



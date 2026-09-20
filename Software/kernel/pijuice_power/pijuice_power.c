// SPDX-License-Identifier: GPL-2.0
/*
 * pijuice_power - virtual power_supply device for the PiJuice HAT.
 *
 * The PiJuice battery lives behind I2C and has no in-kernel driver, so tools
 * that read /sys/class/power_supply/ (btop, upower, GNOME, ...) never see it.
 * This module registers a battery-type power_supply named "pijuice" whose
 * properties are plain writable sysfs attrs. The userspace pijuice_sys daemon
 * pushes live values into them each poll; readers get a normal battery.
 *
 * ponytail: deliberately dumb value store, no I2C in the kernel. The daemon
 * already talks to the HAT as root; duplicating that in-kernel buys nothing.
 */

#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>

/* Nothing is known until the daemon writes: no phantom half-full battery. */
static int capacity;
static int status = POWER_SUPPLY_STATUS_UNKNOWN;
static int present;
static int voltage_now;		/* microvolts */
static int current_now;		/* microamps */
static int temp;		/* tenths of a degree Celsius */
static int charge_full;		/* microamp-hours, learned or profile capacity */
static int charge_full_design;	/* microamp-hours, from the HAT battery profile */
static int cycle_count;		/* equivalent full cycles from battery history */
static int health = POWER_SUPPLY_HEALTH_UNKNOWN;
static int time_to_empty;	/* seconds; 0 = unknown */

static enum power_supply_property pijuice_props[] = {
	POWER_SUPPLY_PROP_STATUS,
	POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_TECHNOLOGY,
	POWER_SUPPLY_PROP_CAPACITY,
	POWER_SUPPLY_PROP_VOLTAGE_NOW,
	POWER_SUPPLY_PROP_CURRENT_NOW,
	POWER_SUPPLY_PROP_TEMP,
	/* Readers that ignore CAPACITY (e.g. wf-panel-pi batt) need charge_*. */
	POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN,
	POWER_SUPPLY_PROP_CHARGE_FULL,
	POWER_SUPPLY_PROP_CHARGE_NOW,
	POWER_SUPPLY_PROP_CYCLE_COUNT,
	POWER_SUPPLY_PROP_HEALTH,
	POWER_SUPPLY_PROP_TIME_TO_EMPTY_NOW,
};

static int pijuice_get_property(struct power_supply *psy,
				enum power_supply_property psp,
				union power_supply_propval *val)
{
	switch (psp) {
	case POWER_SUPPLY_PROP_STATUS:
		val->intval = status;
		break;
	case POWER_SUPPLY_PROP_PRESENT:
		val->intval = present;
		break;
	case POWER_SUPPLY_PROP_TECHNOLOGY:
		val->intval = POWER_SUPPLY_TECHNOLOGY_LIPO;
		break;
	case POWER_SUPPLY_PROP_CAPACITY:
		val->intval = capacity;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		val->intval = voltage_now;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		val->intval = current_now;
		break;
	case POWER_SUPPLY_PROP_TEMP:
		val->intval = temp;
		break;
	case POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN:
		val->intval = charge_full_design;
		break;
	case POWER_SUPPLY_PROP_CHARGE_FULL:
		val->intval = charge_full;
		break;
	case POWER_SUPPLY_PROP_CHARGE_NOW:
		/* Derived, so it can never disagree with capacity. */
		val->intval = (int)((long long)charge_full * capacity / 100);
		break;
	case POWER_SUPPLY_PROP_CYCLE_COUNT:
		val->intval = cycle_count;
		break;
	case POWER_SUPPLY_PROP_HEALTH:
		val->intval = health;
		break;
	case POWER_SUPPLY_PROP_TIME_TO_EMPTY_NOW:
		val->intval = time_to_empty;
		break;
	default:
		return -EINVAL;
	}
	return 0;
}

static int pijuice_set_property(struct power_supply *psy,
				enum power_supply_property psp,
				const union power_supply_propval *val)
{
	union power_supply_propval cur;

	/* The daemon rewrites every attribute each poll; only a real change
	 * is worth a uevent (upower and desktops wake on each one). */
	if (!pijuice_get_property(psy, psp, &cur) && cur.intval == val->intval)
		return 0;

	switch (psp) {
	case POWER_SUPPLY_PROP_STATUS:
		status = val->intval;
		break;
	case POWER_SUPPLY_PROP_PRESENT:
		present = val->intval;
		break;
	case POWER_SUPPLY_PROP_CAPACITY:
		capacity = val->intval;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		voltage_now = val->intval;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		current_now = val->intval;
		break;
	case POWER_SUPPLY_PROP_TEMP:
		temp = val->intval;
		break;
	case POWER_SUPPLY_PROP_CHARGE_FULL:
		charge_full = val->intval;
		break;
	case POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN:
		charge_full_design = val->intval;
		break;
	case POWER_SUPPLY_PROP_CYCLE_COUNT:
		cycle_count = val->intval;
		break;
	case POWER_SUPPLY_PROP_HEALTH:
		health = val->intval;
		break;
	case POWER_SUPPLY_PROP_TIME_TO_EMPTY_NOW:
		time_to_empty = val->intval;
		break;
	default:
		return -EINVAL;
	}
	power_supply_changed(psy);
	return 0;
}

static int pijuice_property_is_writeable(struct power_supply *psy,
					 enum power_supply_property psp)
{
	switch (psp) {
	case POWER_SUPPLY_PROP_STATUS:
	case POWER_SUPPLY_PROP_PRESENT:
	case POWER_SUPPLY_PROP_CAPACITY:
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
	case POWER_SUPPLY_PROP_CURRENT_NOW:
	case POWER_SUPPLY_PROP_TEMP:
	case POWER_SUPPLY_PROP_CHARGE_FULL:
	case POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN:
	case POWER_SUPPLY_PROP_CYCLE_COUNT:
	case POWER_SUPPLY_PROP_HEALTH:
	case POWER_SUPPLY_PROP_TIME_TO_EMPTY_NOW:
		return 1;
	default:
		return 0;
	}
}

static const struct power_supply_desc pijuice_desc = {
	.name = "pijuice",
	.type = POWER_SUPPLY_TYPE_BATTERY,
	.properties = pijuice_props,
	.num_properties = ARRAY_SIZE(pijuice_props),
	.get_property = pijuice_get_property,
	.set_property = pijuice_set_property,
	.property_is_writeable = pijuice_property_is_writeable,
};

static struct power_supply *pijuice_psy;
static struct platform_device *pijuice_pdev;

static int __init pijuice_power_init(void)
{
	struct power_supply_config cfg = {};

	pijuice_pdev = platform_device_register_simple("pijuice-power", -1,
						       NULL, 0);
	if (IS_ERR(pijuice_pdev))
		return PTR_ERR(pijuice_pdev);

	pijuice_psy = power_supply_register(&pijuice_pdev->dev, &pijuice_desc,
					    &cfg);
	if (IS_ERR(pijuice_psy)) {
		platform_device_unregister(pijuice_pdev);
		return PTR_ERR(pijuice_psy);
	}
	return 0;
}

static void __exit pijuice_power_exit(void)
{
	power_supply_unregister(pijuice_psy);
	platform_device_unregister(pijuice_pdev);
}

module_init(pijuice_power_init);
module_exit(pijuice_power_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("PiJuice virtual power_supply, fed by the pijuice_sys daemon");
MODULE_AUTHOR("PiJuice");
MODULE_VERSION("1.3");

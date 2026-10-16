#!/usr/bin/env python3
"""Configurable, terminal-only macOS desktop automation."""
import argparse
import json
import math
import random
from pathlib import Path
import secrets
import subprocess
import sys
import time

DEFAULT_CONFIG = Path(__file__).with_name("desktop_cycle.json")


def seconds(value):
    number = float(value)
    if not math.isfinite(number) or number < 1:
        raise ValueError("Timing values must be finite and at least one second.")
    return number


def validate(config):
    apps = config.get("applications")
    if not isinstance(apps, list) or not apps or any(
        not isinstance(app, str) or not app.strip() for app in apps
    ):
        raise ValueError("applications must be a nonempty list of application names.")
    # Older configurations used a fixed switch_seconds value. Use the new
    # requested random defaults unless a range is explicitly configured.
    config.pop("switch_seconds", None)
    config.setdefault("switch_min_seconds", 5)
    config.setdefault("switch_max_seconds", 10)
    config.setdefault("start_immediately", True)
    config.setdefault("mouse_seconds", 1)
    for key in ("idle_seconds", "switch_min_seconds", "switch_max_seconds", "action_seconds", "mouse_seconds"):
        config[key] = seconds(config[key])
    if config["switch_max_seconds"] < config["switch_min_seconds"]:
        raise ValueError("Maximum switch delay must be at least the minimum.")
    for key in ("move_mouse", "tap_shift", "start_immediately"):
        if not isinstance(config[key], bool):
            raise ValueError(f"{key} must be true or false.")
    return config


def setup(path):
    print("Enter application names as shown in Finder; one per line.")
    print("Press Enter on an empty line when finished.")
    apps = []
    while True:
        name = input("Application: ").strip()
        if not name:
            if apps:
                break
            print("Enter at least one application.")
        elif name not in apps:
            apps.append(name)

    def timing(prompt, default):
        while True:
            try:
                return seconds(input(f"{prompt} [{default}]: ").strip() or default)
            except ValueError as exc:
                print(exc)

    def yes_no(prompt, default):
        while True:
            answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("yes", "y", "no", "n"):
                return answer in ("yes", "y")
            print("Enter y or n.")

    print("Startup mode: 1 = start immediately; 2 = wait for idle time.")
    while True:
        mode = input("Choose startup mode [1]: ").strip() or "1"
        if mode in ("1", "2"):
            break
        print("Enter 1 or 2.")
    idle_seconds = timing("Idle seconds before starting/resuming after manual input", 30)
    switch_min = timing("Minimum seconds between application switches", 5)
    while True:
        switch_max = timing("Maximum seconds between application switches", 10)
        if switch_max >= switch_min:
            break
        print("Maximum must be at least the minimum.")
    config = {
        "applications": apps,
        "start_immediately": mode == "1",
        "idle_seconds": idle_seconds,
        "switch_min_seconds": switch_min,
        "switch_max_seconds": switch_max,
        "mouse_seconds": timing("Seconds between mouse movements", 1),
        "action_seconds": timing("Seconds between optional Shift taps", 10),
        "move_mouse": yes_no("Move the pointer smoothly around the main screen", True),
        "tap_shift": yes_no("Tap Shift (may affect shortcuts or accessibility features)", False),
    }
    path.write_text(json.dumps(validate(config), indent=2) + "\n", encoding="utf-8")
    print(f"Saved {path}. Setup does not start automation.")


def run(config):
    if sys.platform != "darwin":
        raise RuntimeError("This script supports macOS only.")
    try:
        import Quartz as Q
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt,
        )
        from AppKit import NSWorkspace
        from CoreFoundation import (
            CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
            CFRunLoopGetCurrent, CFRunLoopRunInMode, CFRunLoopRemoveSource,
            CFMachPortInvalidate, kCFRunLoopDefaultMode,
        )
    except ImportError as exc:
        raise RuntimeError("Install dependencies: python3 -m pip install -r requirements.txt") from exc

    workspace = NSWorkspace.sharedWorkspace()
    paths = []
    for app in config["applications"]:
        path = workspace.fullPathForApplication_(app)
        if not path:
            raise ValueError(f"Application not found: {app}. Run setup again with its Finder name.")
        paths.append(str(path))
    if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
        raise RuntimeError(
            "Accessibility permission is denied. A macOS permission prompt has been requested. "
            "Enable the application named by macOS in System Settings > Privacy & Security > Accessibility. "
            "For an integrated terminal, this may be your IDE rather than Warp or Terminal. "
            "Fully quit that host app (Cmd+Q), reopen it, and run the script again. "
            f"Python executable: {Path(sys.executable).resolve()}"
        )

    marker = secrets.randbits(62)
    last_input = time.monotonic()
    if config["start_immediately"]:
        last_input -= config["idle_seconds"]
    monitoring_failed = False
    active = False
    event_types = [
        Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp,
        Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp,
        Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp,
        Q.kCGEventLeftMouseDragged, Q.kCGEventRightMouseDragged,
        Q.kCGEventOtherMouseDragged, Q.kCGEventScrollWheel,
        Q.kCGEventKeyDown, Q.kCGEventKeyUp, Q.kCGEventFlagsChanged,
    ]

    def callback(proxy, kind, event, context):
        nonlocal last_input, monitoring_failed
        if kind in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
            monitoring_failed = True
        elif Q.CGEventGetIntegerValueField(event, Q.kCGEventSourceUserData) != marker:
            # Other automation also pauses us; no keys or input contents are stored.
            last_input = time.monotonic()
        return event

    tap = Q.CGEventTapCreate(
        Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap, Q.kCGEventTapOptionListenOnly,
        sum(1 << kind for kind in event_types), callback, None,
    )
    if tap is None:
        raise RuntimeError("Cannot monitor input. Enable Input Monitoring for your terminal/Python in System Settings > Privacy & Security, then restart the terminal.")
    source = CFMachPortCreateRunLoopSource(None, tap, 0)
    loop = CFRunLoopGetCurrent()
    CFRunLoopAddSource(loop, source, kCFRunLoopDefaultMode)
    Q.CGEventTapEnable(tap, True)

    def post(event):
        Q.CGEventSetIntegerValueField(event, Q.kCGEventSourceUserData, marker)
        Q.CGEventPost(Q.kCGSessionEventTap, event)

    index = 0
    movement = None
    next_switch = next_action = next_mouse = 0
    if config["start_immediately"]:
        print("Starting immediately. Manual input pauses automation. Ctrl+C stops.")
    else:
        print(f"Waiting for {config['idle_seconds']:g} seconds idle. Ctrl+C stops.")
    try:
        while True:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
            if monitoring_failed or not Q.CGEventTapIsEnabled(tap):
                raise RuntimeError("Input monitoring stopped; automation stopped for safety.")
            now = time.monotonic()
            if now - last_input < config["idle_seconds"]:
                if active:
                    print("Input detected: paused.")
                active = False
                movement = None
                continue
            if not active:
                print("Automation active.")
                active = True
                next_switch = next_action = next_mouse = now
            if now >= next_switch:
                # No shell interpolation; names are resolved before automation starts.
                subprocess.run(["/usr/bin/open", "-a", paths[index]], check=True, timeout=5)
                delay = random.uniform(config["switch_min_seconds"], config["switch_max_seconds"])
                print(f"Activated: {config['applications'][index]} (next switch in {delay:.1f}s)")
                index = (index + 1) % len(paths)
                next_switch = time.monotonic() + delay
                # Drain pending manual events before sending mouse or key input.
                continue
            if now >= next_mouse and config["move_mouse"]:
                point = Q.CGEventGetLocation(Q.CGEventCreate(None))
                bounds = Q.CGDisplayBounds(Q.CGMainDisplayID())
                # Stay away from the menu bar, Dock, and hot corners.
                margin_x = min(80, bounds.size.width / 4)
                margin_y = min(80, bounds.size.height / 4)
                target = (
                    random.uniform(bounds.origin.x + margin_x,
                                   bounds.origin.x + bounds.size.width - margin_x),
                    random.uniform(bounds.origin.y + margin_y,
                                   bounds.origin.y + bounds.size.height - margin_y),
                )
                movement = ((point.x, point.y), target, now,
                            min(1.5, config["mouse_seconds"] * 0.8))
                next_mouse = now + config["mouse_seconds"]
            if now >= next_action:
                if config["tap_shift"]:
                    # Always release immediately; never type text into an application.
                    down = Q.CGEventCreateKeyboardEvent(None, 56, True)
                    up = Q.CGEventCreateKeyboardEvent(None, 56, False)
                    try:
                        post(down)
                    finally:
                        post(up)
                next_action = now + config["action_seconds"]
            if movement is not None:
                start, target, started, duration = movement
                progress = min(1.0, (now - started) / duration)
                eased = progress * progress * (3 - 2 * progress)
                point = tuple(a + (b - a) * eased for a, b in zip(start, target))
                post(Q.CGEventCreateMouseEvent(
                    None, Q.kCGEventMouseMoved, point, Q.kCGMouseButtonLeft,
                ))
                if progress >= 1:
                    movement = None
    finally:
        Q.CGEventTapEnable(tap, False)
        CFRunLoopRemoveSource(loop, source, kCFRunLoopDefaultMode)
        CFMachPortInvalidate(tap)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "run", "check"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(args.config)
        else:
            if not args.config.exists():
                if args.command == "run":
                    print("No configuration found. Let's create it first.")
                    setup(args.config)
                else:
                    raise ValueError("No configuration found. Run: python3 desktop_cycle.py setup")
            config = validate(json.loads(args.config.read_text(encoding="utf-8")))
            if args.command == "check":
                print(json.dumps(config, indent=2))
            else:
                run(config)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

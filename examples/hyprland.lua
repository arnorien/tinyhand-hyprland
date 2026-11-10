-- TinyHand for Hyprland — keybindings (Lua config, Hyprland >= 0.57).
-- Paste into ~/.config/hypr/hyprland.lua and adjust the path to tinyhand.py.
--
-- The daemon starts on the first Super+T. While the hand is on:
-- click = tap, drag = draw, Esc = clear the drawing (or close the hand if there is none),
-- Super+Ctrl+Alt+Esc = emergency release.

local tinyhand = "python " .. os.getenv("HOME") .. "/tinyhand-hyprland/tinyhand.py"

hl.bind("SUPER + T", hl.dsp.exec_cmd(tinyhand .. " toggle")) -- hand on/off
hl.bind("SUPER + SHIFT + T", hl.dsp.exec_cmd(tinyhand .. " pick")) -- pick a hand / settings (rofi)
hl.bind("SUPER + ALT + T", hl.dsp.exec_cmd(tinyhand .. " through")) -- real clicks on/off (the hand stays)
hl.bind("SUPER + CTRL + T", hl.dsp.exec_cmd(tinyhand .. " clear")) -- clear the drawing

# Changelog

## 0.3.2

- The page follows the language of your browser or phone (when the language setting is Auto).

## 0.3.1

- Ready-made images built by GitHub (amd64, aarch64): installing no longer builds on your machine.
- Uses half of the CPU cores by default, to leave room for Home Assistant.

## 0.3.0

- Outside the add-on: the web page asks for the Home Assistant address and token (with a link to create it) and tests them. No file to edit. SwimWatch now starts without a token instead of stopping.

## 0.2.0

- Swim alerts to chosen phones, with an "It's us (pause)" and a "False alarm" button.
- Critical alerts switch (`input_boolean.swimwatch_critical_alerts`), off by default.
- Pause: `input_boolean.swimwatch_pause` and `input_select.swimwatch_pause_mode` (until we get out = back on after 20 min with no movement, or until turned back on), pause bar in the web page, reminder while paused.
- No label questions while paused.

## 0.1.0

First version: setup page (camera, sensors, water zone, phones), RT-DETR + CLIP analysis on CPU, learning from labels (phone buttons or web page), Home Assistant entities and `swimwatch_analysis` event.

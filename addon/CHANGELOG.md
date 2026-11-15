# Changelog

## 0.4.1

- Night hours are typed as 24-hour HH:MM (the browser's time field showed AM/PM).

## 0.4.0

- Pool season switch (`input_boolean.swimwatch_season`): off in winter, no analysis, no questions, no live video.
- Critical alerts schedule (`input_select.swimwatch_critical_when`): always, at night, when nobody is home (`zone.home`), or both. The `input_boolean.swimwatch_critical_alerts` switch stays the master.
- Night alert: someone at the pool at night (not swimming), at most every 10 minutes. Night hours in the setup page (22:00 to 07:00 by default).
- Real entities through MQTT when a broker exists (Mosquitto add-on): a device per camera with swimming, people in the water, probability, last analysis and an image entity with the last analyzed picture. Without MQTT nothing changes.
- Ready-made dashboard card to copy in the setup page.

## 0.3.3

- The water zone is found automatically (UperNet ConvNeXt, MIT license), then adjusted: drag a point, click near an edge to add one, double-click to remove one, undo.

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

# SwimWatch

Open **SwimWatch** in the sidebar. The setup opens by itself the first time:

1. **Camera**: click the camera that sees the pool.
2. **When to look**: the motion and person sensors of that camera are already checked. With no sensor, SwimWatch looks every few minutes.
3. **Water zone**: click around the water (inside of the pool), point by point.
4. **Phones**: check the phones (Home Assistant app) that get a question when SwimWatch is not sure. Long-press the notification to answer.

The first start downloads the AI models (about 800 MB). The page shows "loading the models" until they are ready.

Entities (camera id `pool`): `binary_sensor.pool_swimming`, `sensor.pool_people_in_water`, `sensor.pool_last_analysis`, plus the event `swimwatch_analysis`.

Label pictures in the **To label** tab (keys 1 to 4). The more you label, the better it gets.

⚠️ SwimWatch is not a safety device and is not a drowning alarm.

More: https://github.com/frazz1234/swimwatch

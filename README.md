# SwimWatch 🏊

**Is someone swimming in the pool?** SwimWatch looks at a camera you already have in [Home Assistant](https://www.home-assistant.io/) and tells you, with a sensor you can use in automations.

- **Any camera** that shows in Home Assistant: Eufy, Reolink, Unifi, Frigate, RTSP, ONVIF…
- **100 % local.** The AI runs on your own machine, CPU only. No cloud, no subscription, no picture leaves your home.
- **Learns your pool.** When it is not sure, your phone gets the picture with 4 buttons: *Swimming / Person nearby / Animal / Nothing*. Every answer makes it better.
- **Simple setup.** Pick the camera, check the motion sensors, click around the water, pick your phones. Done.

> ⚠️ **SwimWatch is not a safety device.** It can miss people and it is not a drowning alarm. Never rely on it to watch children or anyone near water.

**Status: early version (0.1).** The setup page, the analysis and the Home Assistant entities are tested with a standalone Docker container. Installing through the Home Assistant add-on store has not been tested on a real install yet. Feedback welcome.

[Français plus bas](#français)

## Install (Home Assistant add-on)

You need Home Assistant OS or Supervised (the ones with **Add-ons / Apps**).

1. Click this button, or in Home Assistant go to **Settings → Add-ons → Add-on store → ⋮ → Repositories** and add `https://github.com/frazz1234/swimwatch`:

   [![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ffrazz1234%2Fswimwatch)

2. Install **SwimWatch**, then **Start**. The first install builds it on your machine (10 to 30 minutes, it downloads about 1 GB).
3. Open **SwimWatch** in the sidebar. The setup opens by itself:
   1. **Camera**: click the one that sees the pool.
   2. **When to look**: the motion and person sensors of that camera are already checked. No sensor? It looks every few minutes instead.
   3. **Water zone**: click around the water, point by point.
   4. **Phones**: check the phones (Home Assistant app) that get a question when SwimWatch is not sure.

The first start downloads the AI models (about 800 MB) into the add-on's own storage.

## What you get in Home Assistant

For a camera named "Pool" (id `pool`):

| Entity | Meaning |
|---|---|
| `binary_sensor.pool_swimming` | `on` while someone is swimming (stays on 10 min after the last time it was seen) |
| `sensor.pool_people_in_water` | how many people are in the water |
| `sensor.pool_last_analysis` | time of the last analysis, with its probability |
| event `swimwatch_analysis` | after each analysis: `camera`, `swimming`, `probability`, `persons_in_water`, `persons_near`, `animals`, `uncertain` |

Example automation:

```yaml
automation:
  - alias: Pool in use
    triggers:
      - trigger: state
        entity_id: binary_sensor.pool_swimming
        to: "on"
    actions:
      - action: notify.notify
        data:
          message: Someone is swimming in the pool.
```

## How it works

1. A motion or person sensor turns on, or the "every N minutes" timer fires.
2. SwimWatch takes the picture from Home Assistant. For battery cameras that only film on demand (e.g. Eufy), it can open the live video for 8 seconds and take 4 pictures. It never cuts a live video someone else is watching on the same base station.
3. **[RT-DETR v2](https://huggingface.co/PekingU/rtdetr_v2_r50vd)** finds people and animals. A person whose box sits in the water zone counts as "in the water".
4. **[CLIP](https://github.com/mlfoundations/open_clip)** turns the pool area into a "fingerprint" of the scene.
5. **Learning**: the fingerprint is compared with the pictures you already labeled. When a very similar picture was labeled before, your label wins. A new kind of picture falls back on the detector.
6. Not sure? It sends your phone the picture with 4 buttons. You can also label many pictures at once in the web page (keys 1 to 4).

About 1 to 1.5 s per picture on a 2015 quad-core CPU. It only runs when something moves.

## Without the add-on (Docker, Windows, Linux, macOS)

SwimWatch can run on any computer that can reach Home Assistant.

1. In Home Assistant: your profile → **Security** → **Long-lived access tokens** → create one.
2. Install:

   ```bash
   git clone https://github.com/frazz1234/swimwatch && cd swimwatch
   python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   pip install ./addon/app
   ```

3. Next to where you run it, create `config.yaml` (see [config.example.yaml](config.example.yaml)) with your Home Assistant URL, and `.env` with `HA_TOKEN=<your token>`.
4. Run `swimwatch --config config.yaml`, then open `http://<this computer>:8765` and follow the setup.

Docker: `docker build -t swimwatch addon && docker run -d -p 8765:8765 -v $PWD/data:/data -e HA_URL=http://homeassistant.local:8123 -e HA_TOKEN=... swimwatch` (set `HA_URL` to your Home Assistant address).

Windows: [windows/](windows/) has a start script and `install-task.ps1`, which starts SwimWatch at boot (run it as admin).

If `import torch` fails on Windows with *Error loading ...\shm.dll*, install the latest [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe). Without admin rights: `pip install msvc-runtime`, then copy the `.dll` files from `.venv` into `.venv\Lib\site-packages\torch\lib`.

Outside the add-on, the web page has no login: keep it on your home network, or set `web.password` in `config.yaml`.

## Settings file (optional)

Everything is set in the web page. `config.yaml` is only needed outside the add-on (Home Assistant address), or for special cases:

- `detector.model`: `PekingU/rtdetr_v2_r18vd` is twice as fast (Raspberry Pi), a bit less sure.
- `cameras[].live`: your own service to open the live video (e.g. a script).
- `notify.min_gap_minutes`, `sensor.hold_minutes`, `keep_days`, `threads`.

## Privacy

Pictures and labels stay in the add-on's storage (`/data`). Unlabeled events are deleted after 30 days. Labeled ones are kept, because they are what SwimWatch learned from. Notification pictures are put in Home Assistant's media folder (`/media/swimwatch`).

## Credits and licenses

SwimWatch is under the [MIT license](LICENSE). It uses:

- RT-DETR v2 by PekingU ([Apache-2.0](https://huggingface.co/PekingU/rtdetr_v2_r50vd)), through Hugging Face Transformers (Apache-2.0)
- OpenCLIP ([MIT](https://github.com/mlfoundations/open_clip/blob/main/LICENSE)) with LAION-2B weights ([MIT](https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K))
- PyTorch (BSD-3), FastAPI (MIT), aiohttp (Apache-2.0)

Ideas and fixes are welcome: open an issue or a pull request.

---

## Français

**Quelqu'un se baigne dans la piscine ?** SwimWatch regarde une caméra que tu as déjà dans Home Assistant et te le dit avec un capteur.

- **N'importe quelle caméra** visible dans Home Assistant.
- **100 % local** : l'IA tourne chez toi, sans abonnement, aucune image ne sort de la maison.
- **Apprend ta piscine** : quand il n'est pas sûr, ton téléphone reçoit l'image avec 4 boutons (*Baignade / Personne à côté / Animal / Rien*).
- **Installation simple** : choisis la caméra, coche les capteurs de mouvement, clique autour de l'eau, choisis tes téléphones.

> ⚠️ **SwimWatch n'est pas un dispositif de sécurité.** Il peut manquer quelqu'un. Ne t'y fie jamais pour surveiller des enfants ou quiconque près de l'eau.

**Installer :** Home Assistant → **Paramètres → Modules complémentaires → Boutique → ⋮ → Dépôts**, ajoute `https://github.com/frazz1234/swimwatch`. Installe **SwimWatch** et démarre-le. Ouvre-le dans la barre de gauche et suis les 4 étapes. La page est en français si Home Assistant est en français.

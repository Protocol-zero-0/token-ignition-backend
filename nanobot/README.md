# nanobot/

This directory is the runtime home for the nanobot audit-agent container.

Contents:

- `Dockerfile` — pip-installs `nanobot-ai` and boots `nanobot gateway`.
- `nanobot.config.json` — **auto-generated** at `./setup.sh` time by
  `../scripts/render_nanobot_config.py`. Do not edit by hand; edit
  `../config.yaml` instead and re-run `./setup.sh`.

The real config file is mounted read-only into the container at
`/root/.nanobot/config.json` — see `../docker-compose.yml`.

## Notes

- If the upstream `nanobot-ai` package changes its config schema, update
  the builder in `../scripts/render_nanobot_config.py`. That script is the
  single source of translation from our YAML convention to nanobot's JSON.
- Nanobot channels (Telegram / Discord / WeChat / ...), memory, and the
  "dream" skill-discovery module are all **disabled** by the rendered
  config. The audit agent must remain stateless and reproducible.

# hermes-assist

Carries Home Assistant Assist chat messages to the household Hermes assistant, with the sender's Home Assistant user id.

- `custom_components/hermes_assist/`: Home Assistant integration (a conversation agent). Install with HACS (custom repository) or copy the folder. Config: ingress URL and Assist key.
- `ingress/household_ingress.py`: box-side service (stdlib only). `POST /v1/assist` checks the user id against `members.json`, then calls the household Hermes API server with the member's name. `POST /v1/delegate` (separate key) takes cards from personal Hermes. `GET /health` needs no key.

Messages without a member's user id (voice satellites, automations, strangers) are refused with a plain sentence and never forwarded.

Tests: `python3 -m unittest discover -s tests -p 'test_ingress.py'` (ingress, stdlib) and `uv run --with-requirements requirements-test.txt python -m pytest tests/test_conversation.py` (integration; pytest-socket blocks the ingress tests, so they run separately).

## Photos

A member uploads a photo in the Home Assistant Media panel (Local media > Household > their name) and asks in Assist within 15 minutes. `hermes_assist` attaches up to 3 of that member's photos (shrunk to 2048 px) to the question; the ingress forwards them to household Hermes as `image_url` parts. Sent photos move to `sent/` and are deleted after 30 days. PDFs are not supported; HEIC files that Pillow cannot read are reported in the question text and left in place.

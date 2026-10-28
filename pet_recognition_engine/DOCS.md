# Pet Recognition Engine

The engine of the Pet Recognition integration. It finds the animals in each
picture the integration sends, crops them, and turns each of them into an
embedding: a list of numbers that stays close for pictures of the same
animal. Everything runs on your own hardware.

The engine keeps no data. Pictures stay in memory while they are analysed,
and only the engine's API key is stored, in the app's data folder. The
integration holds your pets, their examples and the history.

## Installation

1. Install this app and start it. The first build downloads and checks the
   two models, about 125 MB, and takes a few minutes.
2. Install the Pet Recognition integration and restart Home Assistant.
3. Home Assistant shows **Discovered: Pet Recognition** in **Settings**,
   **Devices & services**. Select **Add**: the engine's address and key are
   filled in for you.

## Configuration

**API key** (optional): leave it empty. The engine creates its own key on
the first start and gives it to the integration, and that key is never
shown.

## Adding the integration by hand

If Home Assistant does not discover the engine, the app's log says so and
gives the address to enter, such as `http://<hostname>:8765`. Then:

1. Set the **API key** option to a key of at least 16 characters, and
   restart the app.
2. Add the Pet Recognition integration, and enter that address and key.

## Without the Supervisor

With Home Assistant in a container, build and run the same image with
Docker, then add the integration by hand with the engine's URL and key:

```sh
docker build -t pet-recognition-engine .
docker run -d --name pet-recognition-engine -p 8765:8765 \
  -e API_KEY=<a key of at least 16 characters> pet-recognition-engine
```

The engine speaks plain HTTP. Keep its port on your local network, or put
it behind a TLS proxy.

## Models

- Detection: YOLOX-S, trained on COCO, by Megvii (Apache-2.0).
- Embedding: DINOv2 small, by Meta, in its ONNX export by the ONNX
  Community (Apache-2.0).

Both are downloaded from pinned URLs when the app is built, checked by size
and SHA-256, and tested on a sample picture before the build succeeds.

# Pet Recognition Engine API, version 1

The integration talks to the engine over HTTP. The engine keeps no data: it
finds the animals in a picture, crops them and embeds them, and the
integration does everything else. The bodies of the answers are described by
[`api-v1.schema.json`](api-v1.schema.json), which the tests of both sides
check.

## Compatibility

Version 1 only grows by additions: new routes, new optional parameters, new
keys in answers. Clients ignore keys they do not know. A change that breaks
clients comes as `/v2`, served next to `/v1` for at least one release.

## Authentication

Every route except `/health` needs the engine's API key:

```http
Authorization: Bearer <key>
```

A missing or wrong key gets `401` with `WWW-Authenticate: Bearer`.

## Routes

| Route | Body | Answer |
|---|---|---|
| `GET /health` | | `{"status": "ok"}`, without a key |
| `GET /v1/info` | | The API version, the app version, the embedding model, the species and the limits |
| `POST /v1/analyze` | A picture | The animals of the picture, each with its box, crop and embedding |
| `POST /v1/embed` | A picture of one animal | Its embedding |

Pictures are the raw body of the request: JPEG, PNG or WebP, at most
`max_bytes` bytes and `max_pixels` pixels, as given by `/v1/info`. They are
turned upright from their EXIF orientation, and never stored.

### `POST /v1/analyze`

Optional query parameters:

- `min_area`: the smallest share of the picture an animal covers, from 0 to
  less than 1. Default 0.01.
- `limit`: the most animals returned, the most certain first, from 1 to 20.
  Default 5.
- `species`: the species to look for, separated by commas, among `bird`,
  `cat` and `dog`. Default all.

```json
{
  "width": 1280,
  "height": 720,
  "model": "dinov2-small",
  "animals": [
    {
      "species": "cat",
      "score": 0.9374,
      "box": [312.0, 120.5, 811.2, 698.0],
      "area": 0.3127,
      "crop": "<base64 JPEG>",
      "embedding": [0.0213, -0.0456]
    }
  ]
}
```

`box` is in pixels of the upright picture. `embedding` has a length of 1, up
to rounding; the example shortens it.

### `POST /v1/embed`

The picture is embedded as a whole, without detection: it is meant for crops
that the engine returned before, for example to embed them again with a new
model.

```json
{"model": "dinov2-small", "embedding": [0.0213, -0.0456]}
```

## Errors

Errors are JSON with a stable code:

```json
{"error": {"code": "unreadable_image", "message": "the picture cannot be read"}}
```

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | An empty body, or another malformed request |
| 400 | `invalid_parameter` | An unknown or out of range query parameter |
| 401 | `unauthorized` | A missing or wrong key |
| 404 | `not_found` | An unknown route |
| 405 | `method_not_allowed` | The wrong method, with `Allow` |
| 413 | `too_large` | A body of more than `max_bytes` |
| 422 | `unreadable_image` | Not a JPEG, PNG or WebP, too many pixels, or damaged |
| 503 | `busy` | Another picture is being analysed; try again after `Retry-After` seconds |
| 500 | `internal_error` | The engine failed |

The engine analyses one picture at a time. The integration queues its
pictures and sends the next one when the engine is free.

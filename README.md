# pmsbridge

pmsbridge is a self-hosted gateway that connects AI assistants to your
property management system (Guesty and Hostaway) through the Model Context Protocol (MCP).

You run it on your own Linux server. Your PMS credentials and data stay on that server.

> Status: early development. Not ready for production use yet.

## What it does

- Connect one or more PMS accounts (Guesty, Hostaway) from a small web dashboard.
- Add a single MCP URL, `https://<your-domain>/mcp`, as a connector in your AI assistant.
- Ask about listings, reservations, calendars, guests, conversations, reviews, owners, payments and tasks.
- Actions that change data (for example sending a guest message) are off by default and enabled per connection by an admin.

## Your data stays yours

pmsbridge runs on your server and connects to your own PMS account. It does not send any data to the people who
make it: there is no telemetry, no analytics and no license check. Your PMS credentials and data stay on your
server; what your AI assistant reads through pmsbridge goes to that assistant, under its provider's terms.

You decide what the assistant may do. Actions that change data in your PMS (for example prices or guest messages)
are off until an administrator turns them on, and you are responsible for how you use them. pmsbridge is provided
as is, without warranty.

## Quick start

A one-command installer for Linux servers is in development. See [docs/](docs/index.md) for progress.

## Documentation

- [Documentation index](docs/index.md)
- [Architecture](docs/architecture.md)
- [Design decisions](docs/adr/)

## License

pmsbridge is source-available under the [Elastic License 2.0](LICENSE). You may use, modify and
self-host it, including for your own business. You may not provide it to others as a hosted or
managed service.

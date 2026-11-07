# Documentation

Pick the document that matches what you want to do.

| I want to... | Read |
|--------------|------|
| **Understand** what the agent does and why, with diagrams | [How the agent works](How-the-agent-works.md) |
| **Use** the dashboard, the map or the driver page | [User guide](User-guide.md) |
| **Connect** a program to the agent | [API reference](API-reference.md) |
| **See how it was built**, iteration by iteration, and how each part is tested | [Iteration notes](how-it-works/README.md) |
| **Check how safe it is**, and how to publish it safely | [Security policy](../SECURITY.md), [Security review](how-it-works/iteration-8-security-review.md) |
| **Run it with the platform core** on one computer | [Running with the platform core](Running-with-the-platform-core.md) |
| **Hand it in**: how it was verified, every outside call, the figures (French) | [Rapport d'exécution](Rapport-execution.md) |
| **Know why** a stack or an address field was chosen | [Technical stack decisions](Technical-Stack-Decisions.md), [Adresse de livraison](Adresse-livraison-proposition.md) |

## Quick start

```bash
docker compose up --build -d             # the agent on http://localhost:9000
curl http://localhost:9000/health
```

For real road distances and road lines on the map, prepare the road network once (about ten minutes, see [Navigation and real roads](how-it-works/iteration-7-navigation-and-roads.md)).

Then: the platform activates a company on the agent, the operator generates its access codes (`POST /tenant/{id}/access-codes`), and people sign in at `http://localhost:9000/ui/login?tenant=<company-identifier>`.

To run the platform core locally and activate the agent on it, follow [Running the agent with the platform core](Running-with-the-platform-core.md).

## Pictures

The screenshots used in the guides are in [images](images/).

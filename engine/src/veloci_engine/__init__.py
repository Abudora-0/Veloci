import asyncio


def main() -> None:
    from veloci_engine.cli import main as cli_main

    asyncio.run(cli_main())

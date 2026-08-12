"""Simple CLI tool using Click 7.x API patterns."""
import click


@click.command()
@click.option("--name", prompt="Your name", help="The person to greet.")
@click.option("--count", default=1, help="Number of greetings.")
@click.option("--verbose", is_flag=True, help="Enable verbose output.")
def hello(name, count, verbose):
    """Simple program that greets NAME for a total of COUNT times."""
    for _ in range(count):
        if verbose:
            click.echo(f"Verbose: greeting {name}")
        click.echo(f"Hello, {name}!")


@click.group()
def cli():
    """Main entry point."""
    pass


@cli.command()
@click.argument("filename", type=click.Path(exists=True))
def process(filename):
    """Process a file."""
    click.echo(f"Processing {filename}")


if __name__ == "__main__":
    cli()

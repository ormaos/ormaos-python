"""A Click command line with one group and one command."""

import click


def parse(text):
    try:
        return int(text)
    except ValueError:
        return 0  # not a number counts as zero


@click.group()
def cli():
    pass


@cli.command()
@click.argument("numbers", nargs=-1)
@click.option("--label", default="total")
def add(numbers, label):
    click.echo(f"{label}: {sum(parse(number) for number in numbers)}")


if __name__ == "__main__":
    cli()

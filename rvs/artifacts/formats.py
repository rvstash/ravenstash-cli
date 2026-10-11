"""The public format vocabulary and shared set-valued option parser."""

FORMATS = ("pypi", "npm", "maven", "oci")


def flatten_formats(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item not in FORMATS:
                raise ValueError(f"Unknown or empty format '{item}'. Choose {', '.join(FORMATS)}.")
            if item not in result:
                result.append(item)
    return result

"""Construct address-range test cases without deployment-specific literals."""


def ip(*octets):
    return ".".join(map(str, octets))

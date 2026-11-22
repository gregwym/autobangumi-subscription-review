"""Read bounded bencoded dictionaries from torrents and tracker responses."""


def decode_dict(data):
    pos = nodes = 0

    def read(depth=0):
        nonlocal pos, nodes
        nodes += 1
        if depth > 24 or nodes > 100_000 or pos >= len(data):
            raise ValueError("invalid bencode")
        tag = data[pos : pos + 1]
        if tag.isdigit():
            end = data.index(b":", pos, pos + 10)
            size = data[pos:end]
            if not size.isdigit() or end + 1 + int(size) > len(data):
                raise ValueError("invalid string")
            pos = end + 1 + int(size)
            return data[end + 1 : pos]
        pos += 1
        if tag == b"i":
            end = data.index(b"e", pos, pos + 24)
            num = int(data[pos:end])
            pos = end + 1
            return num
        if tag not in (b"d", b"l"):
            raise ValueError("invalid type")
        result = {} if tag == b"d" else []
        while data[pos : pos + 1] != b"e":
            key = read(depth + 1)
            if tag == b"d":
                if not isinstance(key, bytes) or key in result:
                    raise ValueError("invalid key")
                result[key] = read(depth + 1)
            else:
                result.append(key)
        pos += 1
        return result

    root = read()
    if pos != len(data) or not isinstance(root, dict):
        raise ValueError("invalid dictionary")
    return root

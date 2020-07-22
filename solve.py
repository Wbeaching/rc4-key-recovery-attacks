"""

solve.py
========
Exploits weaknesses in RC4 to mount a chosen-plaintext attack and
recover the private key. It works when ephemeral keys are generated by
concatenating a public nonce before the long-term key (ala Section
4.3, "Attacks on the RC4 stream cipher", Andreas Klein 2006).

See "About:" for more details, server.py for the oracle
implementation, and https://danielwilshirejones.com/rc4-ctr for the
full spiel

Usage:
  solve.py [--server=<url>] [--samples=<int>] [--key_size=<int>] [--counter_size=<int>] [--nonce_size=<int>] [--block_size=<int>] [--cache=<path>]
  solve.py (-h | --help)

Options:
  -h --help             Show this screen.
  --server=<url>        URL of server to attack [default: http://localhost:5000].
  --samples=<int>       Number of samples to take [default: 100000].
  --key_size=<int>      Size of key in bytes [default: 13].
  --counter_size=<int>  Size of block counter in bytes [default: 3].
  --nonce_size=<int>    Size of per-session nonce in bytes [default: 16].
  --block_size=<int>    Size of each block in bytes [default: 48].
  --cache=<path>        If given, will pickle the samples to the given
                        file path. This should save lots of time in
                        future runs.

Example:
  solve.py --server="https://localhost:5000" --cache="samples.csv"

About:
  solve.py is a quick and dirty implementation of Section 4.4 of
  Andreas Klein's "Attacks on the RC4 stream cipher" paper
  (https://engineering.purdue.edu/ece404/Resources/AndreasKlein.pdf).

  This particular attack works against a mock encryption algorithm,
  described in server.py.

  This code expects to have access to a mock HTTP API (like the one
  provided in server.py) that works as an encryption oracle. You can
  give it the parameters of the above function via a GET call to:

    /rc4-ctr/encrypt/<nonce>/<counter>/<plaintext>

  where <nonce> and <plaintext> are hex strings, while counter is a
  simple non-negative integer. An example call might be:

    GET /rc4-ctr/encrypt/710790b2e53bbe3f4da853d64fb513b9/0/3c05a07f9a332132b3...6e0aa1

  The body of the response will be the encrypted block.

  This endpoint acts as an oracle for a chosen-plaintext attack where
  we will recover the private key.

"""


import csv
import docopt
import logging
import requests
import secrets

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from operator import xor

logging.basicConfig(level=logging.INFO)


# Utility/Helper Functions ----------------------------------------------------------- #
def read_cache(path):
    try:
        samples = []
        nonce = None

        with open(path, "r", newline="") as cache_file:
            cache_reader = csv.reader(cache_file)
            for row in cache_reader:
                nonce = bytes.fromhex(row[0])
                samples.append((bytes.fromhex(row[1]), bytes.fromhex(row[2])))

    except Exception as e:
        logging.warning(
            "Failed to load cache, filling a new one in the path specified.",
            exc_info=e,
        )

    return nonce, samples


def write_cache(path, nonce, samples):
    try:
        with open(path, "w", newline="") as cache_file:
            cache_writer = csv.writer(cache_file)
            for counter, keystream in samples:
                cache_writer.writerow([nonce.hex(), counter.hex(), keystream.hex()])

    except Exception as e:
        logging.error("Failed to save cache.", exc_info=e)


def try_convert_bytes_to_string(bs):
    try:
        return bs.decode("utf-8")
    except:
        pass

    try:
        return bs.decode("ascii")
    except:
        pass

    return "<string conversion failed>"


def count_elements(xs):
    counts = {x: 0 for x in xs}

    for x in xs:
        counts[x] += 1

    return counts


def most_common_element(xs):
    counts = count_elements(xs)
    return max(xs, key=counts.get)


def plot_key_character_frequencies(candidate_key_bytes, path):
    """
    Plot a histogram of the given raw list of key candidate samples.
    """
    count_dict = count_elements(candidate_key_bytes)

    keys = [k for k in range(256)]
    counts = [count_dict.get(k, 0) for k in range(256)]

    barplot = sns.barplot(
        data=pd.DataFrame({"key": keys, "count": counts}), x="key", y="count",
    )
    barplot.set_xticklabels(
        barplot.get_xticklabels(), rotation=90, horizontalalignment="right"
    )

    plt.figure(figsize=(40, 8))
    plt.savefig(path)


# Encryption Oracle ---------------------------------------------------------- #
def encrypt(server_url, nonce, counter, plaintext):
    server_url = server_url.rstrip()
    response = requests.get(
        f"{server_url}/rc4-ctr/encrypt/{nonce}/{counter}/{plaintext}"
    )
    return response.text.strip()


# Attack --------------------------------------------------------------------- #
def test_key(nonce, counter, counter_size, key, plaintext, expected_ciphertext):
    """
    Use our own RC4 implementation to test whether our current guess
    is the complete key. This is useful as a stopping condition for
    the search.
    """
    try:
        key_bytes = (
            nonce
            + counter.to_bytes(length=counter_size, byteorder="little", signed=False)
            + key
        )

        cipher = Cipher(
            algorithm=algorithms.ARC4(key_bytes), mode=None, backend=default_backend()
        )
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(plaintext)

        return ciphertext == expected_ciphertext

    except:
        return False


def attack(num_samples, server_url, nonce_size, counter_size, block_size, cache):
    logging.info(
        "Sample a single (nonce, counter, plaintext, ciphertext) record "
        "from the oracle. This will act as our test suite.",
    )
    test_pt = secrets.token_bytes(block_size)
    test_nonce = secrets.token_bytes(nonce_size)
    test_counter = secrets.randbelow(1000)
    test_ct = bytes.fromhex(
        encrypt(server_url, test_nonce.hex(), test_counter, test_pt.hex())
    )

    logging.info(
        "Sample keystreams from %s blocks (with numbered blocks, "
        "constant nonce, randomised plaintexts)",
        num_samples,
    )

    nonce = None
    samples = []

    # Take samples (loading and saving to cache as needed)
    if cache is not None:
        nonce, samples = read_cache(cache)

    if samples == []:
        nonce = secrets.token_bytes(nonce_size)
        for counter in range(num_samples):
            plaintext = secrets.token_bytes(block_size)
            ciphertext = bytes.fromhex(
                encrypt(server_url, nonce.hex(), counter, plaintext.hex())
            )

            # Because we have the plaintext, we can extract the raw keystream!
            keystream = bytes([xor(pt, ct) for pt, ct in zip(plaintext, ciphertext)])

            counter = counter.to_bytes(
                length=counter_size, byteorder="little", signed=False
            )
            samples.append((counter, keystream))

    if cache is not None:
        write_cache(cache, nonce, samples)

    logging.info("Iteratively learn each byte of the key in series.")

    key = b""
    while not test_key(test_nonce, test_counter, counter_size, key, test_pt, test_ct):
        candidate_key_bytes = []

        for counter, keystream in samples:
            known_bytes = nonce + counter + key
            num_known_bytes = len(known_bytes)

            # We know the first num_known_bytes of the key, so we can
            # step through the first num_known_bytes iteratons of RC4s
            # key scheduling algorithm (KSA):
            S = [i for i in range(256)]

            j = 0
            for i in range(num_known_bytes):
                j = (j + S[i] + known_bytes[i]) % 256
                S[i], S[j] = S[j], S[i]

            # Consider the next iteration of the KSA in Python pseudo-code:
            #   i'= i + 1
            #   j' = (j + S[i'] + unknown_key_byte) % 256
            #   S'[i], S'[j] = S[j'], S[i']
            #
            # After these lines execute:
            #   S'[i] = S[j + S[i'] + unknown_key_byte % 256]
            # where i  = num_known_bytes - 1
            #   and i' = num_known_bytes.
            #
            # And we also have concrete values for S and j. The only
            # unknowns are S'[i] and unknown_key_byte (I refer to
            # S'[i] as next_S_i in the code from now on).
            #
            # The paper shows that the probability next_S_i doesn't
            # change during the rest of the KSA is
            # ((n-1)/n)^(n-num_known_bytes). Further, the
            # probability that next_S_i isn't modified in the first
            # num_known_bytes iterations of the keystream generation,
            # similarly, ((n-1)/n)^num_known_bytes. Combining these
            # two tells us that next_S_i should still be unchanged at
            # num_known_bytes-th iteration of the PRNG with prob
            # ((n-1)/n)^n ~= 1/e ~= 0.367).
            #
            # So, we have a relation between S'[i] and
            # unknown_key_byte for which they are the only unknowns.
            # And we know that around a thrid of the time, S[i] still
            # equals S'[i] (next_S_i) at the num_known_bytes-th
            # iteration of the PRNG.
            #
            # Next, theorem 1 in the paper gives us a probabilistic
            # relation between the num_known_bytes-th output of the
            # PRNG and the value of S[i] during that iteration.
            #
            # (Note that this S[i] is equal to S'[i] (next_S_i) about
            # a third of the time, and that the i we are talking about
            # is num_known_bytes. I.e. these are
            # (num_known_bytes+1)-th value inside S at the time).
            #
            # The paper combines these two relations to show that
            #     Prob(next_S_i == num_known_bytes - num_known_bytes-th output_byte)
            #       ~= 1.36/n
            #
            # This means ... that the output_byte that shows up
            # 1.36/256 of the time is the one in the case where S[i]
            # is unchanged from next_S_i. And so we can find that
            # output_byte, calculate next_S_i, then reverse the
            # relation from earlier to find an implied value for
            # unknown_key_byte! Cool stuff.

            # For now we collect candidate key_bytes, using candidate
            # next_S_i's derived from candidate output_bytes. When
            # we've taken a bunch of samples, we'll figure out which
            # one shows up 1.36/n of the time.

            # Fetch the output we care about then derive S'[1] = next_S_i
            output_byte = keystream[num_known_bytes - 1]
            next_S_i = (num_known_bytes - output_byte) % 256

            # So we know S'[i] = S[j + S[i] + key[0] % 256]
            # and we have a value for S'[i], next_S_i
            # So we have next_S_i = S[j + S[i] + key[0] % 256]

            # Find whats inside the brackers of the LHS of the equation:
            pre_i = None
            for x in range(256):
                if S[x] == next_S_i:
                    pre_i = x
                    break

            # Get the unknown_key_byte by equating the indices of S
            # (the stuff in the square brackets).
            key_byte = (pre_i - j - S[num_known_bytes]) % 256

            candidate_key_bytes.append(key_byte)

        # Now we've got a bunch of candidate values for our unknown
        # key byte. Which one occurs 1.36/n = 1.36/256 of the time?
        # Well, there are n-1 = 255 other potential values, and we can
        # assume the rest are all uniformly distributed.  Then all the
        # other possible values will occur less than 1.36/n of the
        # time.
        # So we can just pick the most comon value:
        chosen_key_byte = most_common_element(candidate_key_bytes)
        key += bytes([chosen_key_byte])

        logging.info(
            "Picked %s for %sth key byte: key=0x%s='%s'",
            chosen_key_byte,
            len(key),
            key.hex(),
            try_convert_bytes_to_string(key),
        )

    logging.info(
        "Key=%s='%s' verified working!", key.hex(), key.decode("ascii"),
    )

    return key


if __name__ == "__main__":
    args = docopt.docopt(__doc__)
    key = attack(
        int(args["--samples"]),
        args["--server"],
        int(args["--nonce_size"]),
        int(args["--counter_size"]),
        int(args["--block_size"]),
        args["--cache"],
    )
    print(key)

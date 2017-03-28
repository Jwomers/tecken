# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, you can obtain one at http://mozilla.org/MPL/2.0/.

import time
import logging
from bisect import bisect
import json  # XXX consider try:except import for ujson

import requests

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.core.cache import cache, caches
from django.views.decorators.csrf import csrf_exempt


logger = logging.getLogger('django')
store = caches['store']


class SymbolDownloadError(Exception):
    def __init__(self, status_code, url):
        self.status_code = status_code
        self.url = url


@csrf_exempt
def symbolicate_json(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Must use HTTP POST'}, status=405)
    try:
        json_body = json.loads(request.body.decode('utf-8'))
        if not isinstance(json_body, dict):
            return JsonResponse({'error': 'Not a dict'}, status=400)
    except ValueError as exception:
        return JsonResponse({'error': 'Invalid JSON passed in'}, status=400)

    try:
        stacks = json_body['stacks']
        memory_map = json_body['memoryMap']
        if json_body.get('version') != 4:
            return JsonResponse({'error': 'Expect version==4'}, status=400)
    except KeyError as exception:
        return JsonResponse({'error': 'Missing key JSON "{}"'.format(
            exception
        )}, status=400)

    debug_output = json_body.get('debug')

    response = {
        'symbolicatedStacks': [],
        'knownModules': [False] * len(memory_map),
    }

    # Record the total time it took to symbolicate
    t0 = time.time()

    for stack in stacks:
        response_stack = []
        for module_index, module_offset in stack:
            if module_index < 0:
                try:
                    response_stack.append(hex(module_offset))
                except TypeError:
                    logger.warning('TypeError on ({!r}, {!r})'.format(
                        module_offset,
                        module_index,
                    ))
                    # Happens if 'module_offset' is not an int16
                    # and thus can't be represented in hex.
                    response_stack.append(str(module_offset))
            else:
                symbol_filename = memory_map[module_index][0]
                response_stack.append(
                    "{} (in {})".format(hex(module_offset), symbol_filename)
                )
        response['symbolicatedStacks'].append(response_stack)

    # per request global map of all symbol maps
    all_symbol_maps = {}

    # XXX Food for thought (1)...
    # Perhaps, to save time, use pipelining to fetch ALL symbols that
    # that we have in one big sweep. Or use mget to simply fetch multiple.

    # XXX Food for thought (2)...
    # With the way the stack works, it's a list of lists. Each item
    # points to a symbol name in `memory_map`, which then gets looked up.
    # The current implementation uses a dict. Perhaps that's not necessary.
    # The dict will consume more RAM since it needs to hold ALL symbol maps
    # for all distinct symbols. If we just override the variable each time
    # it changes instead, we can re-use RAM and not need as much oompf
    # from the web server.
    _seen = set()
    _prev = None

    total_stacks = 0
    real_stacks = 0
    cache_lookup_times = []
    cache_lookup_sizes = []
    download_times = []
    download_sizes = []

    for i, stack in enumerate(stacks):
        for j, (module_index, module_offset) in enumerate(stack):
            total_stacks += 1
            if module_index < 0:
                continue
            real_stacks += 1

            symbol_filename, symbol_debug_id = memory_map[module_index]

            _key = (symbol_filename, symbol_debug_id)
            if _key != _prev and _prev:
                if _key in _seen:
                    logger.warning(
                        "We're coming back to a module ({} {}) we've "
                        "looked up before".format(
                            symbol_filename,
                            symbol_debug_id,
                        )
                    )
                    # print(_key)
                    # raise DebugError('A module has been looked up before!')
            _seen.add(_key)
            _prev = _key

            symbol_key = (symbol_filename, symbol_debug_id)
            if symbol_key not in all_symbol_maps:
                # We have apparently NOT looked up this symbol file + ID before
                # symbol_map, found = get_symbol_map(*symbol_key)
                information = get_symbol_map(*symbol_key, debug=debug_output)
                symbol_map = information['symbol_map']
                assert isinstance(symbol_map, dict), symbol_map
                found = information['found']
                if 'cache_lookup_time' in information:
                    cache_lookup_times.append(information['cache_lookup_time'])
                if 'cache_lookup_size' in information:
                    cache_lookup_sizes.append(information['cache_lookup_size'])
                if 'download_time' in information:
                    download_times.append(information['download_time'])
                if 'download_size' in information:
                    download_sizes.append(information['download_size'])

                # When inserting to the function global all_symbol_maps
                # store it as a tuple with an additional value (for
                # the sake of optimization) of the sorted list of ALL
                # offsets as int16s ascending order.
                all_symbol_maps[symbol_key] = (
                    symbol_map,
                    found,
                    sorted(symbol_map)
                )
            symbol_map, found, symbol_offset_list = all_symbol_maps.get(
                symbol_key,
                ({}, False, [])
            )
            signature = symbol_map.get(module_offset)
            if signature is None and symbol_map:
                try:
                    signature = symbol_map[
                        symbol_offset_list[
                            bisect(symbol_offset_list, module_offset) - 1
                        ]
                    ]
                except IndexError:
                    # XXX How can this happen?!
                    logger.warning(
                        "INDEXERROR:",
                        module_offset,
                        bisect(symbol_offset_list, module_offset) - 1
                    )
                    signature = None

            response['symbolicatedStacks'][i][j] = (
                '{} (in {})'.format(
                    signature or hex(module_offset),
                    symbol_filename,
                )
            )
            response['knownModules'][module_index] = found

    t1 = time.time()

    logger.info(
        'The whole symbolication of {} ({} actual) '
        'stacks took {:.4f} seconds'.format(
            total_stacks,
            real_stacks,
            t1 - t0,
        )
    )

    if debug_output:
        response['debug'] = {
            'total_time': t1 - t0,
            'total_stacks': total_stacks,
            'real_stacks': real_stacks,
            'total': {
                'cache_lookups': {
                    'count': len(cache_lookup_times),
                    'time': float(sum(cache_lookup_times)),
                    'size': float(sum(cache_lookup_sizes)),
                },
                'downloads': {
                    'count': len(download_times),
                    'time': float(sum(download_times)),
                    'size': float(sum(download_sizes)),
                }
            }
        }

    return JsonResponse(response)


_marker = object()


def get_symbol_map(filename, debug_id, debug=False):
    cache_key = 'symbol:{}/{}'.format(filename, debug_id)
    information = {
        'cache_key': cache_key,
        # 'symbol_map': {},
    }
    t0 = time.time()
    symbol_map = store.get(cache_key, _marker)
    t1 = time.time()
    if debug:
        information['cache_lookup_time'] = t1 - t0

    # if symbol_map is None:
    #     store.delete(cache_key)
    #     symbol_map = _marker

    if symbol_map is _marker:  # not existant in ccache
        # Need to download this from the Internet.
        log_symbol_cache_miss(cache_key)
        information.update(load_symbol(filename, debug_id))

        # If it can't be downloaded, cache it as an empty result
        # so we don't need to do this every time we're asked to
        # look up this symbol.
        if not information.get('symbol_map'):
            store.set(
                cache_key,
                {},
                settings.DEBUG and 60 or 60 * 60,
            )
            # If nothing could be downloaded, keep it anyway but
            # to avoid having to check if 'symbol_map' is None, just
            # turn it into a dict.
            information['symbol_map'] = {}  # override
            information['found'] = False
        else:
            store.set(
                cache_key,
                information['symbol_map'],
                # When doing local dev, only store it for 100 min
                # But in prod set it to indefinite.
                timeout=settings.DEBUG and 60 * 100 or None
            )
            information['found'] = True
    else:
        if not symbol_map:
            # It was cached but empty. That means it was logged that
            # it was previously attempted but failed.
            # The reason it's cached is to avoid it being looked up
            # again and again when it's just going to continue to fail.
            information['symbol_map'] = {}
            information['found'] = False
        else:
            if debug:
                information['cache_lookup_size'] = len(json.dumps(symbol_map))
            log_symbol_cache_hit(cache_key)
            # If it was in cache, that means it was originally found.
            information['symbol_map'] = symbol_map
            information['found'] = True

    return information


def log_symbol_cache_miss(cache_key):
    cache_key = cache_key
    # This uses memcache
    cache.set(cache_key, 0, 60 * 60 * 24)


def log_symbol_cache_hit(cache_key):
    cache_key = cache_key
    try:
        cache.incr(cache_key)
    except ValueError:
        # If it wasn't in memcache we can't increment this
        # hit, so we have to start from 1.
        cache.set(cache_key, 1, 60 * 60 * 24)


def load_symbol(filename, debug_id):
    information = {}
    t0 = time.time()
    downloaded = download_symbol(filename, debug_id)
    t1 = time.time()
    information['download_time'] = t1 - t0
    if not downloaded:
        return information
    content, url = downloaded
    if not content:
        logger.warning('Downloaded content empty ({!r}, {!r})'.format(
            filename,
            debug_id,
        ))
        return information

    information['download_size'] = len(content)

    # Need to parse it by line and make a dict of of offset->function
    public_symbols = {}
    func_symbols = {}
    line_number = 0
    t0 = time.time()
    for line in content.splitlines():
        line_number += 1
        if line.startswith('PUBLIC '):
            fields = line.strip().split(None, 3)
            if len(fields) < 4:
                logger.warning(
                    'PUBLIC line {} in {} has too few fields'.format(
                        line_number,
                        url,
                    )
                )
                continue
            address = int(fields[1], 16)
            symbol = fields[3]
            public_symbols[address] = symbol
        elif line.startswith('FUNC '):
            fields = line.strip().split(None, 4)
            if len(fields) < 4:
                logger.warning(
                    'FUNC line {} in {} has too few fields'.format(
                        line_number,
                        url,
                    )
                )
                continue
            address = int(fields[1], 16)
            symbol = fields[4]
            func_symbols[address] = symbol
    t1 = time.time()
    information['parse_symbol_time'] = t1 - t0

    # Prioritize PUBLIC symbols over FUNC symbols # XXX why?
    func_symbols.update(public_symbols)
    information['symbol_map'] = func_symbols
    return information


def download_symbol(lib_filename, debug_id):
    """
    XXX
    At the time of writing this downloads with requests.get().
    We should probably change this to do boto lookups instead.
    """
    if lib_filename.endswith('.pdb'):
        symbol_filename = lib_filename[:-4] + '.sym'
    else:
        symbol_filename = lib_filename + '.sym'

    for base_url in settings.SYMBOL_URLS:
        assert base_url.endswith('/')
        url = '{}{}/{}/{}'.format(
            base_url,
            lib_filename,
            debug_id,
            symbol_filename
        )
        logger.info('Requesting {}'.format(url))
        try:
            response = requests.get(url)
        except requests.exceptions.ContentDecodingError as exception:
            logger.warning(
                '{} when downloading {}'.format(
                    exception,
                    url,
                )
            )
            continue
        if response.status_code == 200:  # Note! This includes redirects
            return response.text, url
        elif response.status_code == 404:
            logger.warning('{} 404 Not Found'.format(url))
        else:
            # XXX Need more grace. A download that isn't 200 or 404 means
            # either a *temporary* network operational error or something
            # horribly wrong with the URL.
            raise SymbolDownloadError(response.status_code, url)

    # None of the URLs worked


def hit_ratio(request):
    cache_misses = []
    cache_hits = {}
    count_keys = 0
    for key in store.iter_keys('symbol:*'):
        count = cache.get(key)
        if count is None:
            # It was cached in Redis before we started logging
            # hits in memcache.
            continue
        count_keys += 1
        if count > 0:
            cache_hits[key] = count
        else:
            cache_misses.append(key)

    sum_hits = sum(cache_hits.values())
    sum_misses = len(cache_misses)

    def f(number):
        return format(number, ',')

    output = []
    output.append(
        'Number of keys: {}'.format(f(count_keys))
    )
    output.append(
        'Number of hits: {}'.format(f(sum_hits))
    )
    output.append(
        'Number of misses: {}'.format(f(sum_misses))
    )
    if sum_hits or sum_misses:
        output.append(
            'Ratio of hits: {:.1f}%'.format(
                100 * sum_hits / (sum_hits + sum_misses)
            )
        )
    output.append('')
    return HttpResponse('\n'.join(output), content_type='text/plain')
import ast
import csv
import json
import os
import shlex
import sys
import traceback
from math import copysign
from re import sub as re_sub

import dijkstar
import discord
import config

# where to save a calculated graph
graph_save_path = './data/graph.cache'
safe_graph_save_path = './data/safe_graph.cache'

# systems we don't want fuzzy matching to hit on in fleetping triggers
fuzzy_match_denylist = config.fuzzy_match_denylist

# when fuzzy matching chats to system names, ignore these chars
punctuation_to_strip = config.punctuation_to_strip

# strings to trigger the output of a detailed path. important that none of these collide with systems!
path_terms = config.path_terms

# strings to trigger the output of a detailed path. important that none of these collide with systems!
safe_terms = config.safe_terms

# strings to trigger a search for the closest non-null system
non_null_terms = config.non_null_terms

# strings to trigger a search for the closest ITC
itc_terms = config.itc_terms

# strings to trigger the display of either closest station or hops with stations
station_terms = config.station_terms


# ----- preparation -----

def parse_star_csv():
    stars = {}
    with open('data/stars.csv') as starcsv:
        csvreader = csv.reader(starcsv, quotechar='"')
        next(csvreader)  # skip header row
        for row in csvreader:
            stars[row[0]] = {'region': row[1],
                             'constellation': row[2],
                             'security': float(row[3]),
                             'edges': ast.literal_eval(row[4])}
    return stars


def parse_truesec_csv():
    # stars.csv's security values are "correctly" rounded, but not the way EE does it. see get_rounded_sec()
    stars_truesec = {}
    with open('data/truesec.csv') as trueseccsv:
        csvreader = csv.reader(trueseccsv)
        next(csvreader)  # skip header row
        for row in csvreader:
            stars_truesec[row[0]] = row[1]
    return stars_truesec


def parse_itc_csv():
    itcs = {}
    with open('data/itcs.csv') as itccsv:
        csvreader = csv.reader(itccsv, quotechar='"')
        next(csvreader)  # skip header row
        for row in csvreader:
            itcs[row[0]] = {'planet': row[1],
                            'moon': row[2],
                            'station': row[3]}
    return itcs


def parse_station_json():
    with open('data/npc_stations.json') as stationjson:
        station_json = json.load(stationjson)

    system_id_lookup = {}
    with open('data/mapSolarSystems.csv') as galaxycsv:
        galaxy_map = csv.DictReader(galaxycsv)
        for row in galaxy_map:
            system_id_lookup[row['solarSystemID']] = row['solarSystemName']

    station_systems = {}
    for station in station_json:
        system_id = str(station_json[station]['solar_system_id'])
        system_name = system_id_lookup[system_id]
        if system_name in station_systems:
            station_systems[system_name] += 1
        else:
            station_systems[system_name] = 1

    return station_systems


def generate_graph(stars):
    graph = dijkstar.Graph()
    for star in stars:
        for edge in stars[star]['edges']:
            graph.add_edge(star, edge, 1)

    graph.dump(graph_save_path)
    return graph


def generate_safe_graph(stars):
    safe_graph = dijkstar.Graph()
    for star in stars:
        for edge in stars[star]['edges']:
            edge_sec = get_rounded_sec(edge)
            if get_sec_status(edge_sec) == 'nullsec':
                cost = 10000
            else:
                cost = 1
            safe_graph.add_edge(star, edge, cost)

    safe_graph.dump(safe_graph_save_path)
    return safe_graph


# ----- graph crunching -----

def jump_path(start: str, end: str, avoid_null=False):
    # generate a dijkstar object describing the shortest path
    g = safe_graph if avoid_null else graph
    path = dijkstar.find_path(g, start, end)
    security_dict = jump_path_security(path)
    return {'path': path, 'security': security_dict}


def jump_count(path):
    # the number of jumps between two systems
    return len(path['path'].nodes) - 1  # don't include the starting node


closest_safes = {}


def closest_safe_system(start):
    # breadth first search to identify the closest non-nullsec system
    if start in closest_safes:
        return closest_safes[start]

    visited = []
    queue = [[start]]

    while queue:
        path = queue.pop(0)
        node = path[-1]
        if node not in visited:
            neighbors = stars[node]['edges']
            for neighbor in neighbors:
                new_path = list(path)
                new_path.append(neighbor)
                queue.append(new_path)
                if get_sec_status(get_rounded_sec(neighbor)) != 'nullsec':
                    closest_safes[start] = neighbor
                    return neighbor
            visited.append(node)

    return False


def closest_itcs(start, count):
    visited = []
    queue = [[start]]

    found_itcs = []

    while queue and len(found_itcs) < count:
        path = queue.pop(0)
        node = path[-1]
        if node not in visited:
            neighbors = stars[node]['edges']
            for neighbor in neighbors:
                new_path = list(path)
                new_path.append(neighbor)
                queue.append(new_path)
                if neighbor not in found_itcs and neighbor in itcs:
                    found_itcs.append(neighbor)
            visited.append(node)

    return found_itcs


def closest_stations(start, count):
    visited = []
    queue = [[start]]

    found_stations = []

    while queue and len(found_stations) < count:
        path = queue.pop(0)
        node = path[-1]
        if node not in visited:
            neighbors = stars[node]['edges']
            for neighbor in neighbors:
                new_path = list(path)
                new_path.append(neighbor)
                queue.append(new_path)
                if neighbor not in found_stations and neighbor in stations:
                    if neighbor != start:
                        found_stations.append(neighbor)
            visited.append(node)

    return found_stations[:count]  # there is a bug with 4-way-ties and this is a lazy fix


# ----- system security math -----

def get_sign(x):
    # return 1.0 or -1.0 depending on sign
    return copysign(1, x)


def get_rounded_sec(star: str):
    # EE takes the truesec (float with 5 decimal places),
    # truncates it to two decimal places, then rounds that as expected
    truncated = str(truesec[star])[0:5]
    rounded = round(float(truncated), 1)
    return rounded


def get_sec_status(rounded_sec: float):
    # classify the security level
    if get_sign(rounded_sec) == -1:
        return 'nullsec'
    elif rounded_sec >= 0.5:
        return 'hisec'
    else:
        return 'lowsec'


def jump_path_security(path):
    # tally the security of each hop along the route
    hisec, lowsec, nullsec = 0, 0, 0
    transit_nodes = path.nodes[1:]
    for node in transit_nodes:
        node_sec = get_rounded_sec(node)
        if get_sign(node_sec) == -1.0:
            nullsec += 1
        elif node_sec >= 0.5:
            hisec += 1
        else:
            lowsec += 1
    return {'hisec': hisec, 'lowsec': lowsec, 'nullsec': nullsec}


# ----- string bashing -----

def flatten(system: str):
    # As of 2020-10-19 there are no collisions in the flattened namespace
    return system.lower().replace('0', 'o')


def generate_flat_lookup(stars):
    flat_lookup = {}
    for star in stars:
        flat_lookup[flatten(star)] = star
    return flat_lookup


fuzzy_matches = {}


def try_fuzzy_match(system: str):
    length = len(system)
    if length < 2:
        return False
    if system in fuzzy_matches:
        return fuzzy_matches[system]
    candidates = []
    for star in flat_lookup:
        if star[0:length].lower() == flatten(system):
            candidates.append(flat_lookup[star])
    if candidates:
        fuzzy_matches[system] = candidates
    return candidates


def check_oh_mixup(system: str):
    # did the provided string have a O/0 mixup?
    if system.lower() != fixup_system_name(system).lower():
        return True
    return False


def merge_fuzzy(submission, completion):
    sublen = len(submission)
    return submission[:sublen] + completion[sublen:]


system_fixups = {}


def fixup_system_name(system: str):
    # returns the real name of a star system, or False
    if system in system_fixups:
        return system_fixups[system]
    if system in stars:
        return system
    if system not in stars:
        flat = flatten(system)
        if flat in flat_lookup:
            lookup = flat_lookup[flatten(system)]
            system_fixups[system] = lookup
            return lookup
        else:
            return False


valid_systems = []


def is_valid_system(system: str):
    # memoized boolean version of fixup_system_name
    if system in valid_systems:
        return True
    check = fixup_system_name(system)
    if check:
        valid_systems.append(system)
        return True
    return False


# ----- string formatting -----

def format_path_security(sec_dict: dict):
    # return f"{sec_dict['hisec']} hisec, {sec_dict['lowsec']} lowsec, {sec_dict['nullsec']} nullsec"
    return f"{sec_dict['nullsec']} nullsec"


def format_sec_icon(rounded_sec: float):
    # pick an emoji to represent the security status
    status = get_sec_status(rounded_sec)
    if status == 'hisec':
        return '🟩'
    if status == 'lowsec':
        return '🟧'
    if status == 'nullsec':
        return '🟥'


def format_system_region(start: str, end: str):
    if start in popular_systems:
        return f"`{end}` is in **{stars[end]['region']}**\n"
    elif stars[start]['region'] == stars[end]['region']:
        return f"`{start}` and `{end}` are both in **{stars[start]['region']}**\n"
    else:
        return f"`{start}` is in **{stars[start]['region']}**, `{end}` is in **{stars[end]['region']}**\n"


def format_jump_count(start: str, end: str, avoid_null=False):
    # assemble all of the useful info into a string for Discord
    start_sec = get_rounded_sec(start)
    end_sec = get_rounded_sec(end)
    path = jump_path(start, end, avoid_null)
    return f"`{start}` ({start_sec} {format_sec_icon(start_sec)}) to `{end}` ({end_sec} {format_sec_icon(end_sec)}): " \
           f"**{jump_count(path)} {jump_word(jump_count(path))}** ({format_path_security(path['security'])})"


def format_partial_match(matches: list):
    response = ":grey_question: Multiple partial matches: "
    count = 1
    for match in matches:
        response += f"`{match}` (**{stars[match]['region']}**)"
        if count < len(matches):
            response += ', '
        count += 1
    return response


def format_system(system: str):
    # figure out the actual system being routed to plus any warnings
    guessed_system = False
    canonical_system = False
    oh_mixup = False
    warnings = []
    if is_valid_system(system):
        canonical_system = fixup_system_name(system)
        oh_mixup = check_oh_mixup(system)
    else:
        fuzzy = try_fuzzy_match(system)
        if fuzzy and len(fuzzy) == 1:
            canonical_system = fixup_system_name(fuzzy[0])
            oh_mixup = check_oh_mixup(merge_fuzzy(system, fuzzy[0]))
        elif fuzzy and len(fuzzy) > 1:
            warnings.append(format_partial_match(fuzzy))
        elif not fuzzy:
            warnings.append(format_unknown_system(system))
    if oh_mixup:
        warnings.append(
            format_oh_mixup(merge_fuzzy(system, guessed_system) if guessed_system else system, canonical_system))
    return canonical_system, warnings


def format_path_hops(start: str, end: str, avoid_null=False):
    # generate the full route
    hops = jump_path(start, end, avoid_null)['path'].nodes
    response = "```"
    hop_count = 0
    for hop in hops:
        hop_sec = get_rounded_sec(hop)
        station = "🛰️" if hop in stations else ""
        response += f"{hop_count}){'  ' if hop_count < 10 else ' '}{hop} " \
                    f"({hop_sec}{format_sec_icon(hop_sec)}) {station}\n"
        hop_count += 1
    response += '```'
    return response


def format_multistop_path(legs: list, stops: list, avoid_null=False):
    # generate the full route with indicators for the specified stops
    hops = []
    response = "```"

    leg_count = 0
    for leg in legs:
        if leg_count == 0:
            hops += jump_path(leg[0], leg[1], avoid_null)['path'].nodes
        else:
            hops += jump_path(leg[0], leg[1], avoid_null)['path'].nodes[1:]
        leg_count += 1

    hop_count = 0
    for hop in hops:
        hop_sec = get_rounded_sec(hop)
        station = "🛰️" if hop in stations else ""
        response += f"{hop_count}){'  ' if hop_count < 10 else ' '}" \
                    f"{'🛑 ' if hop in stops[1:-1] and hop_count != 0 and hop_count != len(hops) - 1 else '   '}{hop}" \
                    f" ({hop_sec}{format_sec_icon(hop_sec)}) {station}\n"
        hop_count += 1

    response += "```"
    return response


def format_unknown_system(provided: str):
    return f":question: Unknown system '{provided}'\n"


def format_oh_mixup(provided: str, corrected: str):
    return f":grey_exclamation: `O`/`0` mixup: you said `{provided}`, you meant `{corrected}`\n"


def punc_strip(word: str):
    return re_sub(punctuation_to_strip, '', word)


def jump_word(jumps: int):
    if jumps == 1:
        return 'jump'
    else:
        return 'jumps'


def check_response_length(response: str):
    if len(response) > 1975:
        return response[:1975] + '\nToo long! Truncating...'
    return response


# ----- bot logic -----

def write_log(logic, message):
    if not logging_enabled:
        return
    # plain old stdout print to be caught by systemd or rsyslog
    if isinstance(message.channel, discord.channel.DMChannel):
        source_string = f"DM {message.author.name}#{message.author.discriminator}"
    else:
        source_string = f"{message.guild.name} #{message.channel.name} {message.author.name}#{message.author.discriminator}"
    mention_id = ""
    for term in message.content.split(' '):
        if any(id in term for id in jumpbot_discord_ids + trigger_roles):
            mention_id = term
            break
    print(f"{source_string} -> {mention_id} [{logic}] : '{message.clean_content}'")


def get_help():
    response = ('Jump counts from relevant systems:   `@jumpbot [system]`\n'
                'Jump counts between a specific pair:  `@jumpbot Jita Alikara`\n'
                'Systems with spaces in their name:     `@jumpbot "New Caldari" Taisy`\n'
                'Multi-stop route:                                      `@jumpbot Taisy Alikara Jita`\n'
                'Show all hops in a path:                          `@jumpbot path taisy alikara`\n'
                'Find a safer path (if possible):               `@jumpbot taisy czdj safe`\n'
                'Find the closest non-nullsec system:   `@jumpbot evac czdj`\n'
                'Find the closest ITC:                                `@jumpbot itc taisy`\n'
                'Find the closest NPC station:                 `@jumpbot station UEJX-G`\n'
                'Autocomplete:                                          `@jumpbot alik ostin`\n'
                'Partial match suggestions:                     `@jumpbot vv`\n\n'
                '_jumpbot is case-insensitive_\n'
                '_message <@!137488285688659969> with bugs or suggestions_ :nerd:')
    return response


def calc_e2e(start: str, end: str, include_path=False, avoid_null=False, show_extras=True):
    # return jump info for a specified pair of systems
    response = ""
    warnings = []

    canonical_start, system_warnings = format_system(start)
    if not canonical_start:
        return ''.join(system_warnings) if show_extras else None
    else:
        [warnings.append(s_w) for s_w in system_warnings]

    canonical_end, system_warnings = format_system(end)
    if not canonical_end:
        return ''.join(system_warnings) if show_extras else None
    else:
        [warnings.append(s_w) for s_w in system_warnings]

    if canonical_start == canonical_end:
        return

    if show_extras:
        if len(warnings) > 0:
            response += ''.join(warnings)
        response += format_system_region(canonical_start, canonical_end)

    response += f"{format_jump_count(canonical_start, canonical_end, avoid_null)}"

    if include_path:
        response += format_path_hops(canonical_start, canonical_end, avoid_null)
    if avoid_null:
        safe_path = jump_path(canonical_start, canonical_end, avoid_null=True)
        safe_hops = jump_count(safe_path)
        safe_nulls = safe_path['security']['nullsec']
        unsafe_path = jump_path(canonical_start, canonical_end, avoid_null=False)
        unsafe_hops = jump_count(unsafe_path)
        unsafe_nulls = unsafe_path['security']['nullsec']
        if not include_path:
            response += '\n'
        if safe_nulls < unsafe_nulls:
            response += f"_{unsafe_nulls - safe_nulls} fewer nullsec hops at the cost of {safe_hops - unsafe_hops} " \
                        f"additional {jump_word(safe_hops - unsafe_hops)}_"
        elif safe_nulls == unsafe_nulls:
            response += "_The shortest path is already the safest!_"
        elif safe_nulls > unsafe_nulls:
            response += "_Somehow it's shorter to fly safer? Something is wrong_"
    return response + '\n'


def calc_from_popular(end: str):
    # return jump info for the defined set of interesting/popular systems
    response = ""
    show_extras = True  # region & warnings
    for start in popular_systems:
        result = calc_e2e(start, end, show_extras=show_extras)
        show_extras = False  # only on first loop
        if result:
            response += result
    return response


def calc_multistop(stops: list, include_path=False, avoid_null=False):
    # return jump info for an arbitrary amount of stops
    valid_stops = []
    warnings = []
    for system in [re_sub(punctuation_to_strip, '', s) for s in stops]:
        canonical_system, system_warnings = format_system(system)
        if system_warnings:
            [warnings.append(s_w + '\n') for s_w in system_warnings]
        if canonical_system:
            valid_stops.append(canonical_system)

    if len(valid_stops) < 2:
        return

    candidate_legs = list(zip(valid_stops, valid_stops[1:]))

    legs = []
    for leg in candidate_legs:
        if leg[0] and leg[1] and leg[0] != leg[1]:
            legs.append(leg)

    response = ''.join(set(warnings))  # merge duplicate warnings
    if legs:
        response += format_system_region(valid_stops[0], valid_stops[-1])

    jump_total = 0
    nullsec_total = 0
    for leg in legs:
        path = jump_path(leg[0], leg[1], avoid_null=avoid_null)
        nullsec_total += path['security']['nullsec']
        jump_total += jump_count(path)
        response += calc_e2e(leg[0], leg[1], show_extras=False, avoid_null=avoid_null)
    if jump_total:
        response += f"\n__**{jump_total} {jump_word(jump_total)} total**__ ({nullsec_total} nullsec)"

    if include_path:
        multistop = format_multistop_path(legs, valid_stops, avoid_null=avoid_null)
        if len(response + multistop) > 2000:
            response += "\n_Can't show the full path - too long for a single Discord message_ :("
        else:
            response += format_multistop_path(legs, valid_stops, avoid_null=avoid_null)

    return response


def fleetping_trigger(message):
    response = ""
    words = set([punc_strip(word) for line in message.content.split('\n') for word in line.split(' ')])
    for word in words:
        if is_valid_system(word) and fixup_system_name(word) not in popular_systems:
            # system_sec = get_rounded_sec(fixup_system_name(word))
            # only respond to nullsec fleetping systems. too many false positives.
            # if get_sec_status(system_sec) == 'nullsec':
            response += calc_from_popular(word)
            if len(response) > 1:
                response += '\n'
        else:
            # only check words longer than 3 chars or we start false positive matching english words
            # (e.g. 'any' -> Anyed)
            if len(word) >= 3 and word.lower() not in fuzzy_match_denylist:
                fuzzy = try_fuzzy_match(word)
                if fuzzy and len(fuzzy) == 1 and fixup_system_name(fuzzy[0]) not in popular_systems:
                    system_sec = get_rounded_sec(fixup_system_name(fuzzy[0]))
                    if get_sec_status(system_sec) == 'nullsec':
                        end = format_system(fuzzy[0])[0]
                        response += calc_from_popular(end)
                        if len(response) > 1:
                            response += '\n'
    if response:
        write_log('fleetping', message)
        return response
    else:
        write_log('fleetping-noöp', message)
        return


def closest_safe_response(system: str, include_path=False):
    candidate, warnings = format_system(system)
    if not candidate:
        return ''.join(warnings)
    closest = closest_safe_system(candidate)
    path = jump_path(candidate, closest, avoid_null=False)
    jumps = jump_count(path)
    closest_sec = get_rounded_sec(closest)
    response = f"The closest non-nullsec system to `{candidate}` is `{closest}` " \
               f"({closest_sec} {format_sec_icon(closest_sec)}) " \
               f"(**{jumps} {jump_word(jumps)}**, in **{stars[closest]['region']}**)"
    if include_path:
        response += format_path_hops(candidate, closest, avoid_null=False)
    if warnings:
        response = ''.join(warnings) + response
    return response


def closest_itc_response(system: str, include_path=False):
    itc_count = 3
    candidate, warnings = format_system(system)
    if not candidate:
        return ''.join(warnings)
    closest = closest_itcs(candidate, itc_count)
    if itc_count > 1:
        response = f"The closest {itc_count} ITCs to `{candidate}` are:"
        for itc in closest:
            path = jump_path(candidate, itc, avoid_null=False)
            jumps = jump_count(path)
            itc_sec = get_rounded_sec(itc)
            response += f"\n`{itc}` ({itc_sec} {format_sec_icon(itc_sec)}): " \
                        f"(**{jumps} {jump_word(jumps)}**, in **{stars[itc]['region']}**)"
    else:
        itc = closest[0]
        path = jump_path(candidate, itc, avoid_null=False)
        jumps = jump_count(path)
        itc_sec = get_rounded_sec(itc)
        response = f"The closest {itc_count} ITC to {candidate} is `{itc}` " \
                   f"({itc_sec} {format_sec_icon(itc_sec)}): " \
                   f"(**{jumps} jumps**, in **{stars[itc]['region']}**)`"
    if warnings:
        response = ''.join(warnings) + response
    return response


def closest_station_response(system: str, include_path=False):
    station_count = 3
    candidate, warnings = format_system(system)
    if not candidate:
        return ''.join(warnings)
    if candidate in stations:
        station_word = 'stations' if stations[candidate] > 1 else 'station'
        warnings += f'🛰️ `{candidate}` has **{stations[candidate]}** {station_word}\n'
    closest = closest_stations(candidate, station_count)
    if station_count > 1:
        other_word = 'other ' if candidate in stations else ''
        response = f"The closest {station_count} {other_word}station systems to `{candidate}` are:"
        for station in closest:
            path = jump_path(candidate, station, avoid_null=False)
            jumps = jump_count(path)
            station_sec = get_rounded_sec(station)
            station_word = 'stations' if stations[station] > 1 else 'station'
            response += f"\n`{station}` ({stations[station]} {station_word}) " \
                        f"({station_sec} {format_sec_icon(station_sec)}): " \
                        f"(**{jumps} {jump_word(jumps)}**, in **{stars[station]['region']}**)"
    else:
        station = closest[0]
        path = jump_path(candidate, station, avoid_null=False)
        jumps = jump_count(path)
        station_sec = get_rounded_sec(station)
        response = f"The closest {station_count} station to {candidate} is `{station}` " \
                   f"({station_sec} {format_sec_icon(station_sec)}): " \
                   f"(**{jumps} {jump_word(jumps)}**, in **{stars[station]['region']}**)`"
    if warnings:
        response = ''.join(warnings) + response
    return response


def mention_trigger(message):
    response = False
    try:
        msg_args = shlex.split(message.content)
    except:
        msg_args = re_sub('[\'\"]', '', message.content).split(' ')
    for arg in msg_args:
        if any(id in arg for id in jumpbot_discord_ids):
            # remove the jumpbot mention to allow leading or trailing mentions
            msg_args.remove(arg)

    include_path = False
    avoid_null = False
    if len(msg_args) >= 2:  # figure out if they want us to include all hops in the path
        for arg in msg_args:
            if any(term in arg.lower() for term in path_terms):
                path_string = arg
                msg_args.remove(path_string)
                include_path = True  # "@jumpboth path w-u"
                break

        for arg in msg_args:
            if any(term in arg.lower() for term in safe_terms):
                safe_string = arg
                msg_args.remove(safe_string)
                avoid_null = True  # "@jumpboth safe w-u taisy"
                break

        for arg in msg_args:
            if any(term in arg.lower() for term in non_null_terms):
                non_null_string = arg
                msg_args.remove(non_null_string)
                if len(msg_args) == 1:
                    response = closest_safe_response(msg_args[0], include_path)
                    write_log('evac', message)
                    return response  # "@jumpbot evac czdj"
                else:
                    write_log('error-evac', message)
                    return "?:)?"

        for arg in msg_args:
            if any(term in arg.lower() for term in itc_terms):
                itc_string = arg
                msg_args.remove(itc_string)
                if len(msg_args) == 1:
                    response = closest_itc_response(msg_args[0])
                    write_log('itc', message)
                    return response  # "@jumpbot itc taisy"
                else:
                    write_log('error-itc', message)
                    return "?:)?"

        for arg in msg_args:
            if any(term in arg.lower() for term in station_terms):
                station_string = arg
                msg_args.remove(station_string)
                if len(msg_args) == 1:
                    response = closest_station_response(msg_args[0], include_path)
                    write_log('station', message)
                    return response  # "@jumpbot station uej"
                else:
                    write_log('error-station', message)
                    return "?:)?"

    if len(msg_args) == 1:
        if 'help' in msg_args[0].lower():  # "@jumpbot help"
            response = get_help()
            write_log('help', message)
        else:  # "@jumpbot Taisy"
            response = calc_from_popular(msg_args[0])
            if include_path:
                response += "\n_provide both a start and an end if you want to see the full path :)_"
            write_log('popular', message)
    elif len(msg_args) == 2:  # "@jumpbot Taisy Alikara"
        response = calc_e2e(msg_args[0], msg_args[1], include_path, avoid_null)
        write_log('e2e-withpath' if include_path else 'e2e', message)
    elif len(msg_args) >= 3:  # "@jumpbot D7 jita ostingele
        if len(msg_args) > 24:
            response = '24 hops max!'
            write_log('error-long', message)
        else:
            try:
                response = calc_multistop(msg_args, include_path, avoid_null)
                write_log('multistop-withpath' if include_path else 'multistop', message)
            except Exception as e:
                response = "?:)"
                write_log('error-parse', message)
                print(e, ''.join(traceback.format_tb(e.__traceback__)))
    if not response:
        write_log('error-empty', message)
        response = "?:)?"
    return response


# ----- core -----

def init():
    # set up globals
    global stars
    stars = parse_star_csv()
    global flat_lookup
    flat_lookup = generate_flat_lookup(stars)
    global truesec
    truesec = parse_truesec_csv()
    global itcs
    itcs = parse_itc_csv()
    global stations
    stations = parse_station_json()
    global graph
    if os.path.isfile(graph_save_path):
        graph = dijkstar.Graph.load(graph_save_path)
    else:
        graph = generate_graph(stars)
    global safe_graph
    if os.path.isfile(safe_graph_save_path):
        safe_graph = dijkstar.Graph.load(safe_graph_save_path)
    else:
        safe_graph = generate_safe_graph(stars)
    global popular_systems
    popular_systems = config.popular_systems
    global jumpbot_discord_ids
    jumpbot_discord_ids = config.discord_ids
    global trigger_roles
    trigger_roles = [role[0] for role in config.trigger_roles]
    global logging_enabled
    logging_enabled = config.debug_logging


def main():
    init()

    discord_token = config.discord_token

    if not discord_token or not jumpbot_discord_ids or not popular_systems or not trigger_roles:
        print("[!] Missing environment variable!")
        sys.exit(1)

    client = discord.Client()

    @client.event
    async def on_ready():
        print(f'[+] {client.user.name} has connected to the discord API')
        for guild in client.guilds:
            print(f'[+] joined {guild.name} [{guild.id}]')
        if logging_enabled:
            print("[+] Logging is active!")

    @client.event
    async def on_message(message):
        try:
            if message.author == client.user:
                # ignore ourself
                return

            if any(role in message.content for role in trigger_roles):
                # proactively offer info when an interesting role is pinged
                response = fleetping_trigger(message)
                if response:
                    await message.channel.send(check_response_length(response))

            elif any(id in message.content for id in jumpbot_discord_ids):
                # we were mentioned
                response = mention_trigger(message)
                await message.channel.send(check_response_length(response))

        except Exception as e:
            write_log('error-exception', message)
            print(e, ''.join(traceback.format_tb(e.__traceback__)))

    client.run(discord_token)


if __name__ == '__main__':
    try:
        main()
    finally:
        print("[!] Closing gracefully!")
        print("System fixups:", system_fixups)
        print("Valid systems:", valid_systems)
        print("Fuzzy matches:", fuzzy_matches)

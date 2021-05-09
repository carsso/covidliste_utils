import argparse
import csv
import logging
from urllib.parse import urlparse
import requests
import json
import hashlib
import os
import re
from io import BytesIO
from PIL import Image
from resizeimage import resizeimage
from resizeimage.imageexceptions import ImageSizeError
from bs4 import BeautifulSoup
import config
import slack_sdk

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,%(msecs)d %(levelname)s:%(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger('extract_list')

slack_client = slack_sdk.WebClient(token=config.SLACK_API_BEARER_TOKEN)

pics_mimetypes = {"image/png", "image/jpeg", "image/jpg", "image/gif"}

key_mappings = {
    'Nom complet': 'fullname',
    'Type de personne': 'type',
    'Prénom 👀': 'firstname',
    'Nom 👀': 'lastname',
    'Identité 👀': 'identity',
    'Pseudo slack (si différent du nom complet)': 'nick',
    'Adresse mail (celle utilisée pour le slack)': 'email',
    'Téléphone portable (si numéro français, format français, sinon format international +32 XX...)': 'phone',
    "J'accepte d'être mentionné comme bénévole en public (site + twitter)": 'mention',
    'Votre équipe dans Covidliste': 'team',
    "L'équipe que vous leadez (si vous êtes lead)": 'leading_team',
    'Code postal de résidence': 'res_postcode',
    'Ville de résidence': 'res_city',
    "Code postal d'origine": 'orig_postcode',
    "Ville d'origine": 'orig_city',
    'GitHub (pseudo seulement) 👀': 'github',
    'Linkedin (lien du profil seulement) 👀': 'linkedin',
    'Twitter (pseudo seulement) 👀': 'twitter',
    'Autre pseudo (si vous voulez apparaitre sous un pseudo) 👀': 'nickname',
    'Mini bio 👀': 'bio',
    'Spécialité 👀': 'specialty',
    'Disponibilité': 'dispo',
    "Role dans Covidliste, ce que vous faites quoi (en plus de l'équipe) 👀": 'role',
    'Photo ou avatar sous forme de lien 👀': 'pic',
    "Commentaire autre, si vous ne voulez pas qu'on publie un truc, si vous avez autre chose à dire": 'comment',
}

public = set([x[1] for x in filter(lambda x: '👀' in x[0], key_mappings.items())] + ['picture', 'anon', 'id'])

default_headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.128 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Pragma': 'no-cache',
    'Cache-Control': 'no-cache',
}


def is_filled(string):
    if string and string.strip():
        return True
    else:
        return False


def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--out-json",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--out-pics-folder",
        type=str,
        required=True,
    )
    args, unknown = parser.parse_known_args()

    return args


def get_csv_users():
    if not config.VOLUNTEERS_CSV_URL:
        raise Exception('Missing CSV')
    response = requests.get(config.VOLUNTEERS_CSV_URL, headers=default_headers)
    response.raise_for_status()
    if 200 <= response.status_code <= 209 and "text/csv" in response.headers["content-type"]:
        csv_users = {}
        response_decoded = [line.decode('utf-8') for line in response.iter_lines()]
        r = csv.reader(response_decoded)
        headers = None
        for row in r:
            if not headers and row[0] == 'Nom complet':
                headers = row
            elif not list(filter(None, row)):
                break
            elif headers:
                csv_user = {key_mappings[k]: v for k, v in zip(headers, row) if k}
                person_id = hashlib.sha256(csv_user['email'].encode("utf-8")).hexdigest()
                person_id = hashlib.md5(person_id.encode("utf-8")).hexdigest()
                csv_user['id'] = person_id
                csv_user['is_benevole'] = csv_user['type'] == 'Bénévole'
                csv_user['is_invite_special'] = csv_user['type'] == 'Invité spécial'
                if "email" in csv_user and csv_user["email"]:
                    csv_users[csv_user["email"]] = csv_user

        return csv_users
    else:
        raise Exception(f"Requests error : {response.status_code}")


def get_slack_users():
    logger.info(f"Loading slack users...")
    response = slack_client.users_list(limit=1000)
    users = response["members"]

    response = slack_client.team_billableInfo()
    users_billable_info = response["billable_info"]

    channels = get_slack_channels()
    benevoles_channel_members = channels[config.BENEVOLES_SLACK_CHANNEL]["members"]

    members = {}
    for user in users:
        if not user["is_app_user"] and not user["is_bot"] and user["name"] != "slackbot":
            user["billing_active"] = False
            if user["id"] in users_billable_info:
                user["billing_active"] = users_billable_info[user["id"]]["billing_active"]

            user["is_benevole"] = user["id"] in benevoles_channel_members
            user['is_invite_special'] = False
            if "is_ultra_restricted" in user and user['is_ultra_restricted']:
                user['is_invite_special'] = True
            if "is_restricted" in user and user['is_restricted']:
                user['is_invite_special'] = True

            user["email"] = user["profile"]["email"]
            user_all_channels = {}
            user_public_channels = {}
            user_private_channels = {}
            user_benevoles_channels = {}
            user_missing_benevoles_channels = {}
            for channel_id, channel in channels.items():
                if user["id"] in channel["members"]:
                    user_all_channels[channel_id] = channel
                if channel["is_private"]:  # private channel
                    if channel["name"].startswith("bénévoles_"):
                        if user["id"] in channel["members"]:
                            user_benevoles_channels[channel_id] = channel
                        else:
                            user_missing_benevoles_channels[channel_id] = channel
                    else:
                        if user["id"] in channel["members"]:
                            user_private_channels[channel_id] = channel
                else:
                    if user["id"] in channel["members"]:
                        user_public_channels[channel_id] = channel
            user["all_channels"] = user_all_channels
            user["public_channels"] = user_public_channels
            user["private_channels"] = user_private_channels
            user["benevoles_channels"] = user_benevoles_channels
            user["missing_benevoles_channels"] = user_missing_benevoles_channels
            members[user["profile"]["email"]] = user
    return members


def get_slack_channels():
    logger.info(f"Loading slack channels list...")
    response = slack_client.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=500)
    r_channels = response["channels"]
    channels = {}
    i = 1
    for channel in r_channels:
        i += 1
        channel_id = channel["id"]
        channel_name = channel["name"]
        logger.info(f"Loading slack channel members {channel_id} ({channel_name}) {i}/{len(r_channels) + 1}...")
        response = slack_client.conversations_members(channel=channel_id, limit=500)
        members = response["members"]
        channel["members"] = members
        channels[channel["id"]] = channel
    return channels


def get_slack_user_presence(user):
    response = slack_client.users_getPresence(user=user)
    r_channels = response["channels"]
    channels = {}
    i = 1
    for channel in r_channels:
        i += 1
        channel_id = channel["id"]
        channel_name = channel["name"]
        logger.info(f"Loading slack channel members {channel_id} ({channel_name}) {i}/{len(r_channels) + 1}...")
        response = slack_client.conversations_members(channel=channel_id, limit=500)
        members = response["members"]
        channel["members"] = members
        channels[channel["id"]] = channel
    return channels


def handle_mention(csv_user):
    if not is_filled(csv_user['fullname']) \
            or not is_filled(csv_user['firstname']) \
            or not is_filled(csv_user['lastname']) \
            or not is_filled(csv_user['email']) \
            or not is_filled(csv_user['mention']):
        for k, v in csv_user.items():
            if k not in ["id", "team", "leading_team"]:
                csv_user[k] = ""
        csv_user['anon'] = True
        return csv_user
    elif csv_user['mention'] == 'Non' or csv_user['mention'] == '':
        for k, v in csv_user.items():
            if k not in ["id", "team", "leading_team"]:
                csv_user[k] = ""
        csv_user['anon'] = True
        return csv_user
    elif csv_user['mention'] == 'Oui : uniquement Prénom + 1ère lettre du Nom':
        if csv_user['lastname']:
            csv_user['lastname'] = csv_user['lastname'][0]
        else:
            logger.warning(f"{csv_user['firstname']} {csv_user['email']} {csv_user['slack_id']} -> missing lastname")
    elif csv_user['mention'] == 'Oui : uniquement Prénom':
        csv_user['lastname'] = ''
    elif csv_user['mention'] == 'Oui : uniquement Autre Pseudo':
        csv_user['firstname'] = ''
        csv_user['lastname'] = ''
    elif csv_user['mention'] == 'Autre chose : précisez en commentaire':
        logger.warning(
            f"{csv_user['firstname']} {csv_user['lastname']} {csv_user['slack_id']} -> needs manual attention ({csv_user['comment']})")
        return None
    elif csv_user['mention'] == 'Oui : nom complet':
        # nothing to do
        pass

    csv_user['anon'] = False
    return csv_user


def verify_pic(csv_user):
    src = csv_user['pic']
    if not src:
        return None

    up = urlparse(src)
    if not up.netloc:
        return None

    if 'zupimages.net' in up.netloc and 'viewer.php' in up.path:
        src = 'https://www.zupimages.net/up/%s' % up.query.replace('id=', '')

    r = requests.get(src, headers=default_headers)
    if 200 <= r.status_code <= 209 and r.headers["content-type"] in pics_mimetypes:
        return src
    else:
        logger.warning(
            f"{csv_user['firstname']} {csv_user['lastname']} {csv_user['slack_id']} -> picture url does not point to a valid picture {src}")

    return None


def get_github_pic(csv_user):
    handle = csv_user['github']
    r = requests.get(f'https://github.com/{handle}', headers=default_headers)
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, 'lxml')
        img = soup.select('img.avatar-user.width-full')
        if img:
            src = img[0].get('src')
            return src
    return None


def get_twitter_pic(csv_user):
    handle = csv_user['twitter']
    twitter_headers = default_headers;
    twitter_headers["authorization"] = "Bearer " + config.TWITTER_API_BEARER_TOKEN
    r = requests.get(f'https://api.twitter.com/1.1/users/show.json?screen_name={handle}', headers=twitter_headers)
    if r.status_code == 200:
        twitter_user = r.json()
        if twitter_user and not twitter_user["default_profile_image"] and twitter_user["profile_image_url_https"]:
            src = re.sub(r'_normal\.', '.', twitter_user["profile_image_url_https"])
            return src
    return None


def get_pic(csv_user, pics_folder):
    priority = ['pic', 'twitter', 'linkedin', 'github']
    for field in priority:
        if csv_user[field]:
            pic_name = None
            if field == 'pic':
                pic_name = download_and_crop_pic(csv_user, verify_pic(csv_user), pics_folder)
            elif field == 'github':
                pic_name = download_and_crop_pic(csv_user, get_github_pic(csv_user), pics_folder)
            elif field == 'twitter':
                pic_name = download_and_crop_pic(csv_user, get_twitter_pic(csv_user), pics_folder)

            if pic_name:
                return pic_name

    return None


def download_and_crop_pic(csv_user, pic_url, pics_folder):
    if not pic_url:
        return None
    image_name = "volunteer-" + csv_user['id']
    r = requests.get(pic_url, headers=default_headers)
    if 200 <= r.status_code <= 209 and r.headers["content-type"] in pics_mimetypes:
        with Image.open(BytesIO(r.content)) as image:
            image = image.convert('RGB')
            try:
                cover = resizeimage.resize_cover(image, [200, 200])
                cover.save(pics_folder + os.path.sep + image_name + '.jpg', image.format)
                return image_name + '.jpg'
            except ImageSizeError as e:
                logger.warning(
                    f"{csv_user['firstname']} {csv_user['lastname']} {csv_user['slack_id']} -> picture cannot be cropped : {e.message} - {pic_url}")


def to_json(people, json_file, pics_folder):
    out = []
    for csv_user in people:
        csv_user = handle_mention(csv_user)
        if csv_user:
            if csv_user['linkedin']:
                csv_user['linkedin'] = re.sub(r'(https?://)?www\.linkedin\.com/', 'https://www.linkedin.com/',
                                              csv_user['linkedin'])
            if not csv_user['anon']:
                csv_user['picture'] = ''
                pic_name = get_pic(csv_user, pics_folder)
                if pic_name:
                    csv_user['picture'] = pic_name

            del csv_user['pic']
            out.append({k: v for k, v in csv_user.items() if k in public})

    with open(json_file, 'w') as f:
        json.dump(out, f, sort_keys=True, indent=2)


def check_consistency(csv_users, slack_users):
    is_inconsistent = False
    for csv_email, csv_user in csv_users.items():
        full_name = csv_user['fullname']
        email = csv_email
        if csv_email not in slack_users.keys():
            # user from csv is NOT on slack
            is_inconsistent = True
            logger.warning(f"{email} ({full_name}) is on the CSV list but do not exist on slack")

    for slack_email, slack_user in slack_users.items():
        full_name = slack_user["profile"]["real_name"]
        email = slack_email

        if slack_email in csv_users.keys():
            # user from slack is on CSV
            csv_user = csv_users[slack_email]
            if slack_user['is_benevole'] != csv_user['is_benevole']:
                is_inconsistent = True
                logger.warning(f"{email} ({full_name}) inconsistency between slack and CSV about person type bénévole :"
                               f"CSV:{csv_user['is_benevole']} Slack:{slack_user['is_benevole']}")

            if slack_user['is_invite_special'] != csv_user['is_invite_special']:
                is_inconsistent = True
                logger.warning(f"{email} ({full_name}) inconsistency between slack and CSV about person type invité "
                               f"spécial : CSV:{csv_user['is_invite_special']} Slack:{slack_user['is_invite_special']}")
        else:
            # user from slack is NOT on CSV
            if slack_user["is_benevole"]:
                is_inconsistent = True
                logger.warning(f"{email} ({full_name}) is volunteer on slack and should be in CSV")
            else:
                is_inconsistent = True
                logger.warning(f"{email} ({full_name}) is on slack (but NOT volunteer) and should be in CSV")

        if slack_user['is_benevole']:
            if len(slack_user["missing_benevoles_channels"]) > 0:
                is_inconsistent = True
                channels_names = ", ".join([v["name"] for k, v in slack_user["missing_benevoles_channels"].items()])
                logger.warning(
                    f"{email} ({full_name}) is volunteer on slack but is NOT on bénévoles channels : {channels_names}")
        elif slack_user['is_invite_special']:
            if not (set(slack_user["all_channels"].keys())).issubset(
                    set(config.ALLOWED_SLACK_CHANNELS_FOR_INVITE_SPECIAL)):
                is_inconsistent = True
                channels_names = ", ".join(
                    [v["name"] + " (" + v["id"] + ")" for k, v in slack_user["all_channels"].items()])
                logger.warning(f"{email} ({full_name}) is invite special BUT on NOT ALLOWED CHANNELS. "
                               f"Person is on channels : {channels_names}")
        else:
            if len(slack_user["private_channels"]) > 0:
                is_inconsistent = True
                channels_names = ", ".join([v["name"] for k, v in slack_user["private_channels"].items()])
                logger.warning(
                    f"{email} ({full_name}) is NOT volunteer on slack but is on private channels : {channels_names}")

    for slack_email, slack_user in slack_users.items():
        full_name = slack_user["profile"]["real_name"]
        email = slack_email
        if slack_email in csv_users.keys():
            # user from slack is on CSV
            csv_user = csv_users[slack_email]
            csv_user["slack_id"] = slack_user["id"]
            if not slack_user['is_benevole'] and not slack_user['is_invite_special'] and not slack_user["deleted"]:
                if "is_invited_user" not in slack_user:
                    if slack_user['is_benevole'] == csv_user['is_benevole'] and slack_user['is_invite_special'] == \
                            csv_user['is_invite_special']:
                        if len(slack_user["private_channels"]) <= 0:
                            logger.info(f"[INFO] {email} ({full_name}) is not benevole nor invite special")
            if slack_user['is_benevole'] and not slack_user['billing_active'] and not slack_user["deleted"]:
                channels_names = ", ".join([v["name"] for k, v in slack_user["private_channels"].items()])
                logger.info(f"[INFO] {email} ({full_name}) is billing inactive (not logged-in since two weeks). "
                            f"Person on private channels : {channels_names}")

    if is_inconsistent:
        raise Exception("Inconsistencies found, please fix them first")

    for slack_email, slack_user in slack_users.items():
        full_name = slack_user["profile"]["real_name"]
        email = slack_email
        if slack_user['is_invite_special']:
            channels_names = ", ".join(
                [v["name"] + " (" + v["id"] + ")" for k, v in slack_user["all_channels"].items()])
            logger.info(f"[INFO] {email} ({full_name}) is invite special on slack on channels : {channels_names}")

    count_not_filled = 0
    count_filled = 0
    slack_mention_text = ""
    for csv_email, csv_user in csv_users.items():
        full_name = csv_user['fullname']
        slack_id = csv_user['slack_id']
        email = csv_user['email']
        if csv_user['is_benevole']:
            if not is_filled(csv_user['fullname']) \
                    or not is_filled(csv_user['firstname']) \
                    or not is_filled(csv_user['lastname']) \
                    or not is_filled(csv_user['identity']) \
                    or not is_filled(csv_user['email']) \
                    or not is_filled(csv_user['phone']) \
                    or not is_filled(csv_user['mention']) \
                    or not is_filled(csv_user['team']):
                logger.info(f"{email} ({full_name} #{slack_id}) has not filled the CSV")
                slack_mention_text += f"<@{slack_id}> "
                count_not_filled += 1
            else:
                count_filled += 1
    logger.info(f"Slack mention text ready to copy/paste : {slack_mention_text}")
    logger.info(f"{count_filled} volunteers have filled the CSV")
    logger.info(f"{count_not_filled} volunteers have not filled (or badly filled) the CSV yet")


if __name__ == '__main__':
    args = get_args()
    csv_users = get_csv_users()
    if not csv_users:
        raise Exception("Cannot load data from CSV")
    slack_users = get_slack_users()
    if not slack_users:
        raise Exception("Cannot load data from slack")

    check_consistency(csv_users, slack_users)

    csv_users_filtered_list = (csv_user for csv_user in csv_users.values() if csv_user['is_benevole'])

    to_json(csv_users_filtered_list, args.out_json, args.out_pics_folder)

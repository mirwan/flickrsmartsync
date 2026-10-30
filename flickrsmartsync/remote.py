import html
import json
import os
import re
import sys
import urllib.request
import flickrapi
import logging

logger = logging.getLogger("flickrsmartsync")

#  flickr api keys
KEY = 'f7da21662566bc773c7c750ddf7030f7'
SECRET = 'c329cdaf44c6d3f3'

# number of retries for downloads
RETRIES = 5


def authorize(username=None):
    # One time interactive OAuth, the token is cached in ~/.flickr for unattended runs
    api = flickrapi.FlickrAPI(KEY, SECRET, username=username)
    if api.token_valid(perms='delete'):
        logger.info('Already authorized')
        return
    api.get_request_token(oauth_callback='oob')
    print('Please authorize this app by opening this url:')
    print(api.auth_url(perms='delete'))
    verifier = input('Then enter the verifier code: ').strip()
    api.get_access_token(verifier)
    logger.info('Authorized, token saved')


class Remote(object):

    def __init__(self, cmd_args):
        # Command line arguments
        self.cmd_args = cmd_args
        self.auth_api()

        # Common arguments
        self.args = {'format': 'json', 'nojsoncallback': 1}

        # photo_sets_map[folder] = id
        self.update_photo_sets_map()

    def auth_api(self):
        self.api = flickrapi.FlickrAPI(KEY, SECRET, username=self.cmd_args.username)  # pass username argument to api

        # Never prompt here, the token must have been cached in ~/.flickr by --auth
        if not self.api.token_valid(perms='delete'):
            logger.error('No valid flickr token found, run "flickrsmartsync --auth" once to authorize this app')
            sys.exit(1)

    # custom set builder
    def get_custom_set_title(self, path):
        title = path.split('/').pop()

        if self.cmd_args.custom_set:
            m = re.match(self.cmd_args.custom_set, path)
            if m:
                if not self.cmd_args.custom_set_builder:
                    title = '-'.join(m.groups())
                elif m.groupdict():
                    title = self.cmd_args.custom_set_builder.format(**m.groupdict())
                else:
                    title = self.cmd_args.custom_set_builder.format(*m.groups())
        return title

    # For adding photo to set
    def add_to_photo_set(self, photo_id, folder):
        # If photoset not found in online map create it else add photo to it
        # Always upload unix style
        if self.cmd_args.is_windows:
            folder = folder.replace(os.sep, '/')

        if folder not in self.photo_sets_map:
            photosets_args = self.args.copy()
            custom_title = self.get_custom_set_title(self.cmd_args.sync_path + folder)
            photosets_args.update({'primary_photo_id': photo_id,
                                   'title': custom_title,
                                   'description': folder})
            photo_set = json.loads(self.api.photosets_create(**photosets_args))
            self.photo_sets_map[folder] = photo_set['photoset']['id']
            logger.info('Created set [%s] and added photo' % custom_title)
        else:
            photosets_args = self.args.copy()
            photosets_args.update({'photoset_id': self.photo_sets_map.get(folder), 'photo_id': photo_id})
            result = json.loads(self.api.photosets_addPhoto(**photosets_args))
            if result.get('stat') == 'ok':
                logger.info('Successfully added photo to %s' % folder)
            else:
                logger.error(result)

    # Get photos in a set
    def get_photos_in_set(self, folder, get_url=False):
        photos = {}
        # Always upload unix style
        if self.cmd_args.is_windows:
            folder = folder.replace(os.sep, '/')

        if folder in self.photo_sets_map:
            photoset_args = self.args.copy()
            page = 1
            while True:
                photoset_args.update({'photoset_id': self.photo_sets_map[folder], 'page': page})
                if get_url:
                    photoset_args['extras'] = 'url_o,media'
                page += 1
                photos_in_set = json.loads(self.api.photosets_getPhotos(**photoset_args))
                if photos_in_set['stat'] != 'ok':
                    break

                for photo in photos_in_set['photoset']['photo']:
                    title = photo['title']
                    # add missing extension if not present (take a guess as api original_format argument not working)
                    split = title.split(".")
                    # assume valid file extension is less than or equal to 5 characters and not all digits
                    if len(split) < 2 or len(split[-1]) > 5 or split[-1].isdigit():
                        if photo.get('media') == 'video':
                            title += ".mp4"
                        else:
                            title += ".jpg"
                    if get_url and photo.get('media') == 'video':
                        photo_args = self.args.copy()
                        photo_args['photo_id'] = photo['id']
                        sizes = json.loads(self.api.photos_getSizes(**photo_args))
                        if sizes['stat'] != 'ok':
                            continue

                        original = [s for s in sizes['sizes']['size'] if s['label'].startswith('Video Original') and s['media'] == 'video']
                        if original:
                            photos[title] = original.pop()['source']
                            
                    else:
                        photos[title] = photo['url_o'] if get_url else photo['id']

        return photos

    def get_photo_sets(self):
        return self.photo_sets_map

    def update_photo_sets_map(self):
        # Get your photosets online and map it to your local
        photosets_args = self.args.copy()
        page = 1
        self.photo_sets_map = {}

        while True:
            logger.debug('Getting photosets page %s' % page)
            photosets_args.update({'page': page, 'per_page': 500})
            sets = json.loads(self.api.photosets_getList(**photosets_args))
            page += 1
            if not sets['photosets']['photoset']:
                break

            for current_set in sets['photosets']['photoset']:
                # Make sure it's the one from backup format
                desc = html.unescape(current_set['description']['_content'])

                if self.cmd_args.fix_missing_description and not desc:
                    current_set_title = html.unescape(current_set['title']['_content'])
                    description_update_args = self.args.copy()
                    description_update_args.update({
                        'photoset_id': current_set['id'],
                        'title': current_set_title,
                        'description': current_set_title
                    })
                    logger.info('Set has no description. Updating it to [%s]...' % current_set_title)
                    json.loads(self.api.photosets_editMeta(**description_update_args))
                    logger.info('done')
                    desc = current_set_title

                if desc:
                    self.photo_sets_map[desc] = current_set['id']
                    title = self.get_custom_set_title(self.cmd_args.sync_path + desc)
                    if self.cmd_args.update_custom_set and title != current_set['title']['_content']:
                        update_args = self.args.copy()
                        update_args.update({
                            'photoset_id': current_set['id'],
                            'title': title,
                            'description': desc
                        })
                        logger.info('Updating custom title [%s]...' % title)
                        json.loads(self.api.photosets_editMeta(**update_args))
                        logger.info('done')

    def upload(self, file_path, photo, folder):
        upload_args = {
            # (Optional) The title of the photo.
            'title': photo,
            # (Optional) A description of the photo. May contain some limited HTML.
            'description': folder,
            # (Optional) Set to 0 for no, 1 for yes. Specifies who can view the photo.
            'is_public': 0,
            'is_friend': 0,
            'is_family': 0,
            # (Optional) Set to 1 for Safe, 2 for Moderate, or 3 for Restricted.
            'safety_level': 1,
            # (Optional) Set to 1 for Photo, 2 for Screenshot, or 3 for Other.
            'content_type': 1,
            # (Optional) Set to 1 to keep the photo in global search results, 2 to hide from public searches.
            'hidden': 2
        }

        for i in range(RETRIES):
            try:
                upload = self.api.upload(file_path, None, **upload_args)
                photo_id = upload.find('photoid').text
                self.add_to_photo_set(photo_id, folder)
                return photo_id
            except Exception as e:
                logger.warning("Retrying upload of %s/%s after error: %s" % (folder, photo, e))
        logger.error("Failed upload of %s/%s after %d retries" % (folder, photo, RETRIES))

    def download(self, url, path):
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)   
        for i in range(RETRIES):
            try:
                return urllib.request.urlretrieve(url, path)
            except Exception as e:
                logger.warning("Retrying download of %s after error: %s" % (path, e))
        # failed many times
        logger.error("Failed to download %s after %d retries" % (path, RETRIES))

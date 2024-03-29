import argparse
import bz2
import logging
import multiprocessing
import os
import sys
import time
import xml.sax
from multiprocessing import Process
from threading import Thread
from image_scraper import extract_categories, extract_images, download_images
import re
import pickle

from cleaner import clean_text

logger = logging.getLogger(__name__)
re_mode = 0
replacements = {'[[': '', ']]': '', '==': ''}

HEADER = {'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/42.0.2311.135 Safari/537.36 Edge/12.246"}


# cleaner.py from https://github.com/CyberZHG Git repo:
# https://github.com/CyberZHG/wiki-dump-reader/blob/master/wiki_dump_reader/cleaner.py


class WikiReader(xml.sax.ContentHandler):
    def __init__(self, ns_filter, callback):
        super().__init__()

        self.filter = ns_filter

        self.read_stack = []
        self.read_text = None
        self.read_title = None
        self.read_namespace = None

        self.status_count = 0
        self.callback = callback

    def startElement(self, tag_name, attributes):
        if tag_name == "ns":
            self.read_namespace = None

        elif tag_name == "page":
            self.read_text = None
            self.read_title = None

        elif tag_name == "title":
            self.read_title = ""

        elif tag_name == "text":
            self.read_text = ""

        else:
            return

        self.read_stack.append(tag_name)

    def endElement(self, tag_name):
        if len(self.read_stack) > 0:
            if tag_name == self.read_stack[-1]:
                del self.read_stack[-1]

        if self.filter(self.read_namespace):
            if tag_name == "page" and self.read_text is not None:
                self.status_count += 1
                self.callback((self.read_title, self.read_text))

    def characters(self, content):
        if len(self.read_stack) == 0:
            return None
        try:
            if self.read_stack[-1] == "text":
                self.read_text += content

            if self.read_stack[-1] == "title":
                self.read_title += content

            if self.read_stack[-1] == "ns":
                self.read_namespace = int(content)

        except Exception as e:
            print(f'Could not add to stack {self.read_stack[-1]}\t, {content}\t, {e}')


def process_text(text):
    rep = dict((re.escape(k), v) for k, v in replacements.items())
    pattern = re.compile("|".join(rep.keys()))
    text = pattern.sub(lambda m: rep[re.escape(m.group(0))], text)
    text = text.split('==References==')[0]
    return text


def process_article(aq, fq, iq, eq, image_path, image_download, shutdown):
    while not (shutdown and aq.empty() and fq.empty()):
        page_title, doc = aq.get()
        if image_download:
            image_dict, parsing_error = extract_images(doc, image_path)
            if image_dict:
                iq.put((page_title, image_dict))
            else:
                image_dict = "None"
        else:
            image_dict = "None"
            parsing_error = None
        cat_list = extract_categories(doc)
        text = process_text(doc)
        text = clean_text(text)
        text = text.encode()
        if not cat_list:
            cat_list = []
        # if image_dict:
        #     for v in image_dict.values():
        #         del v['image_url']
        fq.put({"page": page_title, "sentences": text, 'categories': cat_list, "images": image_dict})
        if parsing_error:
            eq.put(parsing_error)


def download_images_queue(iq, eq, img_path, shutdown):
    error = None
    while not (iq.empty() and shutdown):
        title, file = iq.get()
        try:
            response_code, error = download_images(file, img_path, title)
            if not error:
                error = ''
            if response_code == 429:
                print('Too many requests')
                iq.put(file)
                time.sleep(10)
            elif response_code == 404:
                print('File not found for page: ', title, 'and urls: ', file)
                error = error + 'File not found for page: ', title, 'and urls: ', file
            elif response_code == 200:
                continue
            else:
                error = error + 'Unknown file error : File not moved' + ' Response code: ' + response_code + ' File head: ' + title + ' Image: ' + file
                print('Unknown file error : File not moved', response_code, title, file)
        except KeyError as e:
            for v in file.values():
                # print(f"Key error for downloading images {v['image_url']}")
                error = error + "Key error for downloading images" + str(v['image_url'])
        except TypeError as e:
            print('None type for file:\t', file, '\tPage name\t', title)
            error = error + 'None type for file:\t' + str(file) + '\tPage name\t' + str(title)
        except UnboundLocalError as e:
            print('Cannot access img_url value in:', str(file.values()))
            error = error + 'Cannot access img_url value in:', str(file.values())
        except Exception as e:
            for k in file:
                print('Not able to download images. Exception:', {e}, '\t Page name:\t', title, '\tImage Name:\t', k)
                error = error + f"Not able to download images. Exception: " + str(e) + 'Page name:\t' + str(
                    title) + '\t Image Name:\t' + str(k)
        if error != '':
            eq.put(error)


def write_out(fq, shutdown):
    while not (shutdown and fq.empty()):
        line = fq.get()
        pickle.dump(line, out_file)


def parsing_errors(eq, shutdown):
    while not (shutdown and eq.empty()):
        line = eq.get()
        try:
            pickle.dump(line, error_out)
        except Exception as e:
            print(f'Error File not written for: {line}. Exception: {e}')


def display(aq, fq, iq, eq, reader, shutdown):
    if iq:
        while not (shutdown and aq.empty() and fq.empty() and iq.empty()):
            print("Queue sizes: aq={0} fq={1} iq={2} eq={3}. Read: {4}".format(
                aq.qsize(),
                fq.qsize(),
                iq.qsize(),
                eq.qsize(),
                reader.status_count))
            time.sleep(1)
    else:
        while not (shutdown and aq.empty()):
            print("Queue sizes: aq={0} fq={1}. Read: {2}".format(
                aq.qsize(),
                fq.qsize(),
                reader.status_count))
            time.sleep(1)


# def kill_processes():
#     status._stop()
#     for process in processes:
#         processes[process].kill()
#     for write_thread in write_threads:
#         write_threads[write_thread]._stop()


if __name__ == "__main__":
    shutdown = False

    args_parse = argparse.ArgumentParser()

    # args_parse.add_argument("file", help='File name (including path where it is stored')
    # args_parse.add_argument("--output", help='File name (including path where it is stored')
    # args_parse.add_argument("--image_download", help='Whether to download the images associated with the xml file')
    # args = args_parse.parse_args()
    #
    # source = args.file
    # target = args.output if args.output else 'output.json'
    # images_download = args.image_download if args.image_download else False

    source = "D:\\to_do\\100daysproject\\scrapers\\wikiscrapes\\physics\\data\\content\\processed\\with_template" \
             "\\Physics-1.xml"
    images_download = True
    DATA_PATH = 'data'
    TARGET_FILE = 'output.json'
    data_path = os.path.join(DATA_PATH)
    if data_path not in os.listdir():
        os.mkdir(os.path.join(data_path))
    target = os.path.join(DATA_PATH, TARGET_FILE)
    error_file = 'errors'
    ERROR_PATH = os.path.join(DATA_PATH)
    if ERROR_PATH not in os.listdir():
        os.mkdir(os.path.join(ERROR_PATH))
    error = os.path.join(ERROR_PATH, error_file)
    INIT_IMAGE_FOLDER = 'init_images'
    # orig_path = os.getcwd()
    if INIT_IMAGE_FOLDER not in os.listdir(os.path.join(DATA_PATH)):
        os.makedirs(os.path.join(DATA_PATH, INIT_IMAGE_FOLDER))
    image_path = os.path.join(DATA_PATH, INIT_IMAGE_FOLDER)
    manager = multiprocessing.Manager()
    fq = manager.Queue(maxsize=2000)
    aq = manager.Queue(maxsize=2000)
    eq = manager.Queue(maxsize=2000)
    if images_download:
        iq = manager.Queue(maxsize=10000)
        image_downloader = {}
        for i in range(20):
            image_downloader[i] = Thread(target=download_images_queue, args=(iq, eq, image_path, shutdown))
            image_downloader[i].start()

    else:
        iq = None

    if source[-4:] == ".bz2":
        source = bz2.BZ2File(source)
    elif source[-4:] == '.xml':
        source = open(source, 'r', encoding='utf-8')
    else:
        print('File should be either .bz2 or .xml format. Exiting....')
        sys.exit()

    out_file = open(target, "wb+")
    error_out = open(error, 'w+')

    reader = WikiReader(lambda ns: ns == 0, aq.put)

    status = Thread(target=display, args=(aq, fq, iq, eq, reader, shutdown))
    status.start()

    processes = {}
    for i in range(5):
        processes[i] = Process(target=process_article,
                               args=(aq, fq, iq, eq, image_path, images_download, shutdown))
        processes[i].start()

    write_threads = {}
    for i in range(1):
        write_threads[i] = Thread(target=write_out, args=(fq, shutdown))
        write_threads[i].start()

    write_errors = {}
    for i in range(1):
        write_errors[i] = Thread(target=parsing_errors, args=(eq, shutdown))
        write_errors[i].start()

    st_time = time.time()
    try:
        xml.sax.parse(source, reader)
    except Exception as e:
        print('Error', e)
    end_time = time.time()
    time.sleep(10)
    if images_download:
        shutdown = True if (aq.empty() and fq.empty() and iq.empty() and eq.empty()) else False
    else:
        shutdown = True if (aq.empty() and fq.empty() and eq.empty()) else False
    if shutdown:
        print('Initiating shutdown')
        time.sleep(10)
        # pickle.dump(content, out_file)
        try:
            out_file.close()
            print('Output file closed')
        except Exception as e:
            print('Output file could not be closed due to exception: ', e)
        error_out.close()
        print('Tada ! Processing complete. Close the window and continue...')
        print(f'Time for processing: {end_time - st_time}')
        sys.exit()

        # if shutdown:
    #     kill_processes()

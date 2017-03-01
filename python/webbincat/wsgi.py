import ConfigParser
import distutils.spawn
import hashlib
import os
import shutil
import re
import subprocess
import sys
import tempfile
import flask
import zlib
import logging
import idabincat.npkgen

logging.basicConfig(level=logging.DEBUG)

# Make sure bincat is properly installed, and that none of the required files
# reside in your home dir
# To smoke test, run "firejail --private bincat"
# tested with firejail 0.9.40

SHA256_RE = re.compile('[a-fA-F0-9]{64}')
app = flask.Flask(__name__)
API_VERSION = "1.0"

# check existence of binary storage folder
if 'BINARY_STORAGE_FOLDER' not in app.config:
    app.config['BINARY_STORAGE_FOLDER'] = '/tmp/bincat_web'

if not os.path.isdir(app.config['BINARY_STORAGE_FOLDER']):
    app.logger.error("Binary storage folder %s does not exist",
                     app.config['BINARY_STORAGE_FOLDER'])
    sys.exit(1)

# check whether firejail is installed
firejail = distutils.spawn.find_executable("firejail")
if firejail is None:
    app.logger.error("firejail has not been installed")
    sys.exit(1)


@app.route("/")
def home():
    return flask.make_response(
        "This server runs BinCAT, API version %s" % API_VERSION, 200)


@app.route("/version")
def version():
    return API_VERSION


@app.route("/download/<sha256>/<string:compression>", methods=['HEAD', 'GET'])
@app.route("/download/<sha256>", methods=['HEAD', 'GET'],
           defaults={'compression': 'none'})
def download(sha256, compression):
    if not SHA256_RE.match(sha256):
        return flask.make_response(
            "SHA256 expected as endpoint parameter.", 400)
    sha256 = sha256.lower()
    filename = os.path.join(app.config['BINARY_STORAGE_FOLDER'], sha256)
    if os.path.isfile(filename):
        with open(filename, 'r') as f:
            if compression == 'zlib':
                return zlib.compress(f.read())
            else:
                return f.read()
    else:
        return flask.make_response(
            "No file having sha256=%s has been uploaded." % sha256, 404)


@app.route("/add", methods=['PUT'])
def upload():
    if 'file' not in flask.request.files:
        return flask.make_response(
            "This request was expected to include a file named 'file'.", 400)
    f = flask.request.files['file']
    sha256 = store_string_to_file(f.read())
    result = {'sha256': sha256}
    return flask.make_response(flask.jsonify(**result), 200)


def store_string_to_file(s, alt_path=None):
    """
    Write file to storage, with hardlink to alt_path if supplied
    """
    h = calc_sha256(s)
    fname = os.path.join(app.config['BINARY_STORAGE_FOLDER'], h)
    with open(fname, 'w') as f:
        f.write(s)
    if alt_path is not None:
        try:
            os.link(fname, alt_path)
        except OSError:
            # file exists, ignore
            pass
    return h


def calc_sha256(s):
    h = hashlib.new('sha256')
    h.update(s)
    return h.hexdigest().lower()


@app.route("/analyze", methods=['POST'])
def analyze():
    if 'init.ini' not in flask.request.files:
        return flask.make_response(
            "No file named 'init.ini' has been uploaded.", 400)
    result = {}
    init_file = flask.request.files['init.ini']
    init_file.seek(0)

    # validation: valid ini file + referenced binary file has already been
    # uploaded
    config = ConfigParser.RawConfigParser()
    config.optionxform = str
    try:
        config.readfp(init_file)
    except ConfigParser.MissingSectionHeaderError:
        return flask.make_response(
            "Supplied init.ini file format is incorrect (missing section "
            "header).", 400)
    # check required sections are present
    for section in ["binary", "analyzer"]:
        if not config.has_section(section):
            return flask.make_response(
                "No [%s] section in supplied init.ini file." % section, 400)
    # check required values are present
    for section, key in [("binary", "filepath"),
                         ("analyzer", "in_marshalled_cfa_file"),
                         ("analyzer", "store_marshalled_cfa"),
                         ("analyzer", "analysis")]:
        try:
            config.get(section, key)
        except ConfigParser.NoOptionError:
            return flask.make_response(
                "No %s key in [%s] section in supplied init.ini file."
                % (section, key), 400)
    binary_name = config.get('binary', 'filepath').lower()
    analysis_method = config.get('analyzer', 'analysis').lower()
    input_files = [binary_name]
    if analysis_method in ("forward_cfa", "backward"):
        in_marshalled_cfa_file = \
            config.get('analyzer', 'in_marshalled_cfa_file').lower()
        input_files.append(in_marshalled_cfa_file)
    if config.has_section("imports"):
        try:
            headers_fname = config.get("imports", "headers")
            input_files.append(headers_fname)
        except ConfigParser.NoOptionError:
            # this is not mandatory
            pass
    for fname in input_files:
        if not SHA256_RE.match(fname):
            return flask.make_response(
                "Filepath (%s) is not a valid sha256 hex string."
                % fname, 400)
        fpath = os.path.join(app.config['BINARY_STORAGE_FOLDER'], fname)
        if not os.path.exists(fpath):
            return flask.make_response(
                "Input file %s has not yet been uploaded." % fname, 400)
    # ini file references known input files, proceeding
    # I miss python3's tempfile.TemporaryDirectory...
    dirname = tempfile.mkdtemp('bincat-web-analysis')
    app.logger.debug("created %s", dirname)

    cwd = os.getcwd()
    os.chdir(dirname)  # bincat outputs .dot in cwd
    config.set('analyzer', 'out_marshalled_cfa_file', 'cfaout.marshal')
    # prepare input files
    config.write(open(os.path.join(dirname, 'init.ini'), 'w'))
    for fname in input_files:
        os.link(os.path.join(app.config['BINARY_STORAGE_FOLDER'], fname),
                os.path.join(dirname, fname))
    # run bincat
    err, stdout = run_bincat(dirname)

    # gather and store outputs
    stdout_sha256 = store_string_to_file(
        stdout, os.path.join(dirname, 'stdout.txt'))
    result['stdout.txt'] = stdout_sha256

    result['errorcode'] = err
    if config.get('analyzer', 'store_marshalled_cfa') == 'true':
        if os.path.isfile('cfaout.marshal'):
            with open('cfaout.marshal') as f:
                fname = calc_sha256(f.read())
                fpath = os.path.join(app.config['BINARY_STORAGE_FOLDER'],
                                     fname)
                try:
                    os.link('cfaout.marshal', fpath)
                except OSError:
                    # file exists, ignore
                    pass
                result['cfaout.marshal'] = fname
    logfname = os.path.join(dirname, 'analyzer.log')
    if os.path.isfile(logfname):
        with open(logfname) as f:
            fname = calc_sha256(f.read())
            fpath = os.path.join(app.config['BINARY_STORAGE_FOLDER'],
                                 fname)
            try:
                os.link(logfname, fpath)
            except OSError:
                # file exists, ignore
                pass
            result['analyzer.log'] = fname
    else:
        result['analyzer.log'] = ""
    outfname = os.path.join(dirname, 'out.ini')
    if os.path.isfile(outfname):
        with open(outfname) as f:
            fname = calc_sha256(f.read())
            fpath = os.path.join(app.config['BINARY_STORAGE_FOLDER'],
                                 fname)
            try:
                os.link(outfname, fpath)
            except OSError:
                # file exists, ignore
                pass
            result['out.ini'] = fname
    else:
        result['out.ini'] = ""

    os.chdir(cwd)
    # shutil.rmtree(dirname)

    return flask.make_response(flask.jsonify(**result), 200)


@app.route("/convert_to_npk", methods=['POST'])
def convert_to_npk():
    if 'file' not in flask.request.files:
        return flask.make_response(
            "This request was expected to include a file named 'file'.", 400)
    headers_data = flask.request.files['file'].read()
    try:
        npk_fname = idabincat.npkgen.NpkGen().generate_npk(headers_data)
    except idabincat.npkgen.NpkGenException as e:
        result = {'error': str(e), 'status': 'failed'}
        return flask.make_response(flask.jsonify(**result), 500)
    with open(npk_fname, 'r') as f:
        npk_data = f.read()
    sha256 = store_string_to_file(npk_data)
    result = {'sha256': sha256, 'status': 'ok'}
    return flask.make_response(flask.jsonify(**result), 200)


def run_bincat(dirname):
    # do not use chroot: not compatible with grsec
    cmdline = ("%s --nosound --caps.drop=all"
               " --quiet"
               " --private"  # new /root, /home
               " --private-dev"  # new /dev, few devices
               " --private-etc=ld.so.cache,ld.so.conf,ld.so.conf.d"  # new /etc
               " --nogroups"  # no supplementary groups
               " --noroot"  # new user namespace
               " --nonewprivs"  # NO_NEW_PRIVS
               " --seccomp"  # default seccomp blacklist
               " --net=none"  # no network
               " --whitelist=%s"  # only allow current analysis dir from /tmp
               " -- ") % (firejail, dirname)
    cmdline += "bincat init.ini out.ini analyzer.log"
    err = 0
    try:
        out = subprocess.check_output(
            cmdline.split(' '),
            stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        err = exc.returncode
        out = exc.output

    return err, out


if __name__ == "__main__":
    app.run()

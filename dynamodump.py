#!/usr/bin/env python2
"""
    Simple backup and restore script for Amazon DynamoDB using boto to work similarly to mysqldump.

    Suitable for DynamoDB usages of smaller data volume which do not warrant the usage of AWS
    Data Pipeline for backup/restores/empty.

    dynamodump supports local DynamoDB instances as well (tested with dynalite).
"""

import argparse
import json
import logging
import os
import shutil
import threading
import datetime
import errno
import fnmatch
import sys
import time
import re
import zipfile
import tarfile
import urllib2
import botocore
import boto3


JSON_INDENT = 2
AWS_SLEEP_INTERVAL = 10  # seconds
LOCAL_SLEEP_INTERVAL = 1  # seconds
BATCH_WRITE_SLEEP_INTERVAL = 0.15  # seconds
MAX_BATCH_WRITE = 25  # DynamoDB limit
SCHEMA_FILE = "schema.json"
DATA_DIR = "data"
MAX_RETRY = 6
LOCAL_REGION = "local"
LOG_LEVEL = "INFO"
DATA_DUMP = "dynamodump"
OUTPUT_DIR = "/tmp/dynamorestore/"
RESTORE_WRITE_CAPACITY = 25
THREAD_START_DELAY = 1  # seconds
CURRENT_WORKING_DIR = os.getcwd()
DEFAULT_PREFIX_SEPARATOR = ","
MAX_NUMBER_BACKUP_WORKERS = 25
METADATA_URL = "http://169.254.169.254/latest/meta-data/"
LOG_FORMAT = "%(levelname)-8s %(asctime)-15s %(message)s"


def _get_aws_client(service, endpoint=None):
    """
    Build connection to some AWS service.
    """

    aws_region = args.region if args.region else os.getenv("AWS_DEFAULT_REGION")

    # Fallback to querying metadata for region
    if not aws_region:
        try:
            azone = urllib2.urlopen(METADATA_URL + "placement/availability-zone",
                                    data=None, timeout=5).read().decode()
            aws_region = azone[:-1]
        except urllib2.HTTPError as e:
            logging.exception("Error determining region used for AWS client.  Typo in code?\n\n" + str(e))
            sys.exit(1)
    if args.profile:
        session = boto3.Session(profile_name=args.profile)
        client = session.client(service, region_name=aws_region, endpoint_url=endpoint)
    else:
        if (args.assumedAccountId):
            if not args.assumedRoleName:
                logging.error("You should specify an asumed role name for the provided asumed account id")
                sys.exit(1)
            else:
                sts_client = boto3.client('sts')
                assumedRoleObject = sts_client.assume_role(
                    RoleArn="arn:aws:iam::%s:role/%s" % (args.assumedAccountId, args.assumedRoleName),
                    RoleSessionName="AssumeRoleDynamoBAckup"
                )
                credentials = assumedRoleObject['Credentials']
        else:
            credentials = {
                'AccessKeyId': args.accessKey,
                'SecretAccessKey': args.secretKey,
                'SessionToken': args.sessionToken
            }

        client = boto3.client(
            service,
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            endpoint_url=endpoint,
            region_name=aws_region
        )

    return client


def get_table_name_by_tag(tag):
    """
    Using provided connection to dynamodb and tag, get all tables that have provided tag

    Profile provided and, if needed, used to build connection to STS.
    """

    matching_tables = []
    all_tables = []
    sts = _get_aws_client("sts")
    dynamo = _get_aws_client("dynamodb")
    account_number = sts.get_caller_identity().get("Account")
    paginator = dynamo.get_paginator("list_tables")
    tag_key = tag.split("=")[0]
    tag_value = tag.split("=")[1]

    get_all_tables = paginator.paginate()
    for page in get_all_tables:
        for table in page["TableNames"]:
            all_tables.append(table)
            logging.debug("Found table " + table)

    for table in all_tables:
        table_arn = "arn:aws:dynamodb:{}:{}:table/{}".format(args.region, account_number, table)
        table_tags = dynamo.list_tags_of_resource(
            ResourceArn=table_arn
        )
        for found_tag in table_tags["Tags"]:
            if found_tag["Key"] == tag_key:
                logging.debug("Checking table " + table + " tag " + found_tag["Key"])
                if found_tag["Value"] == tag_value:
                    matching_tables.append(table)
                    logging.info("Matched table " + table)

    return matching_tables


def do_put_bucket_object(bucket, bucket_object):
    """
    Put object into bucket.  Only called if we've also created an archive file with do_archive()

    Bucket must exist prior to running this function.
    profile could be None.
    bucket_object is file to be uploaded
    """

    s3 = _get_aws_client("s3")
    logging.info("Uploading backup to S3 bucket " + bucket)
    try:
        s3.upload_file(bucket_object, bucket, bucket_object,
                       ExtraArgs={
                           "ServerSideEncryption": "AES256"
                       })
    except botocore.exceptions.ClientError as e:
        logging.exception("Failed to put file to S3 bucket\n\n" + str(e))
        sys.exit(1)


def _do_splitext(s):
    """
    remove extension from s3 file names
    """

    base_name = os.path.splitext(s)
    if base_name[-1] == ".bz2":
        base_name = os.splitext(base_name[0])

    return base_name[0]


def do_get_s3_archive(bucket, table, archive, separator):
    """
    Fetch latest file named filename from S3

    Bucket must exist prior to running this function.
    filename is args.dumpPath.  File would be "args.dumpPath" with suffix .tar.bz2 or .zip
    """

    s3 = _get_aws_client("s3")

    # Make sure bucket exists before continuing
    try:
        s3.head_bucket(
            Bucket=bucket
        )
    except botocore.exceptions.ClientError as e:
        logging.exception("S3 bucket " + bucket + " does not exist. "
                          "Can't get backup file\n\n" + str(e))
        sys.exit(1)

    try:
        contents = s3.list_objects_v2(
            Bucket=bucket,
            Prefix=args.dumpPath
        )
    except botocore.exceptions.ClientError as e:
        logging.exception("Issue listing contents of bucket " + bucket + "\n\n" + str(e))
        sys.exit(1)

    # Script will always overwrite older backup.  Bucket versioning stores multiple backups.
    # Therefore, just get item from bucket based on table name since that's what we name the files.
    content_files = [_do_splitext(os.path.basename(file_path)) for file_path in [d["Key"] for d in contents["Contents"]]]
    if table == "*":
        matching_tables = content_files
    elif separator and table.find(separator) != -1:
        matching_tables = list(set([table_name for table_names in [fnmatch.filter(content_files, table_name) for table_name in table.split(separator)] for table_name in table_names]))
    else:
        matching_tables = list(set(fnmatch.filter(content_files, table)))

    if not matching_tables:
        logging.exception("Unable to find file to restore from.  "
                          "Confirm the name of the table you're restoring.")
        sys.exit(1)
    elif len(matching_tables) > 1:
        for d in contents["Contents"]:
            if _do_splitext(os.path.basename(d["Key"])) in matching_tables:
                filename = d["Key"]
                output_file = OUTPUT_DIR + os.path.basename(filename)
                logging.info("Downloading file " + filename + " to " + output_file)
                s3.download_file(bucket, filename, output_file)
                # Extract archive based on suffix
                if tarfile.is_tarfile(output_file):
                    try:
                        logging.info("Extracting tar file...")
                        with tarfile.open(name=output_file, mode="r:bz2") as a:
                            a.extractall(path=".")
                    except tarfile.ReadError as e:
                        logging.exception("Error reading downloaded archive\n\n" + str(e))
                    except tarfile.ExtractError as e:
                        # ExtractError is raised for non-fatal errors on extract method
                        logging.error("Error during extraction: " + str(e))

                # Assuming zip file here since we're only supporting tar and zip at this time
                else:
                    try:
                        logging.info("Extracting zip file...")
                        with zipfile.ZipFile(output_file, "r") as z:
                            z.extractall(path=".")
                    except zipfile.BadZipFile as e:
                        logging.exception("Problem extracting zip file\n\n" + str(e))
    else:
        for d in contents["Contents"]:
            if _do_splitext(os.path.basename(d["Key"])) == matching_tables[0]:
                filename = d["Key"]
                output_file = OUTPUT_DIR + os.path.basename(filename)
                logging.info("Downloading file " + filename + " to " + output_file)
                s3.download_file(bucket, filename, output_file)
                # Extract archive based on suffix
                if tarfile.is_tarfile(output_file):
                    try:
                        logging.info("Extracting tar file...")
                        with tarfile.open(name=output_file, mode="r:bz2") as a:
                            a.extractall(path=".")
                        break
                    except tarfile.ReadError as e:
                        logging.exception("Error reading downloaded archive\n\n" + str(e))
                        sys.exit(1)
                    except tarfile.ExtractError as e:
                        # ExtractError is raised for non-fatal errors on extract method
                        logging.error("Error during extraction: " + str(e))

                # Assuming zip file here since we're only supporting tar and zip at this time
                else:
                    try:
                        logging.info("Extracting zip file...")
                        with zipfile.ZipFile(output_file, "r") as z:
                            z.extractall(path=".")
                        break
                    except zipfile.BadZipFile as e:
                        logging.exception("Problem extracting zip file\n\n" + str(e))
                        break


def do_archive(archive_type, dump_path):
    """
    Create compressed archive of dump_path.

    Accepts archive_type of zip or tar and requires dump_path, directory added to archive
    """

    archive_base = dump_path

    if archive_type.lower() == "tar":
        archive = archive_base + ".tar.bz2"
        try:
            logging.info("Creating tar file " + archive + "...")
            with tarfile.open(name=archive, mode="w:bz2") as a:
                for root, dirs, files in os.walk(archive_base):
                    for file in files:
                        a.add(os.path.join(root, file))
                return True, archive
        except tarfile.CompressionError as e:
            logging.exception("compression method is not supported or the data cannot be"
                              " decoded properly.\n\n" + str(e))
            sys.exit(1)
        except tarfile.TarError as e:
            logging.exception("Error creating tarfile archive.\n\n" + str(e))
            sys.exit(1)

    elif archive_type.lower() == "zip":
        try:
            logging.info("Creating zip file...")
            archive = archive_base + ".zip"
            with zipfile.ZipFile(archive, "w") as z:
                for root, dirs, files in os.walk(archive_base):
                    for file in files:
                        z.write(os.path.join(root, file))
                return True, archive
        except zipfile.BadZipFile as e:
            logging.exception("Problem creating zip file\n\n" + str(e))
            sys.exit(1)
        except zipfile.LargeZipFile:
            logging.exception("Zip file would be too large.  Update code to use Zip64 to continue.")
            sys.exit(1)

    else:
        logging.error("Unsupported archive format received.  Probably shouldn't have "
                      "made it to this code path.  Skipping attempt at creating archive file")
        return False, None


def get_table_name_matches(dynamo, table_name_wildcard, separator):
    """
    Find tables to backup
    """

    all_tables = []
    last_evaluated_table_name = None
    table_list = dynamo.list_tables()
    all_tables.extend(table_list["TableNames"])
    while True:
        try:
            last_evaluated_table_name = table_list["LastEvaluatedTableName"]
            table_list = dynamo.list_tables(ExclusiveStartTableName=last_evaluated_table_name)
            all_tables.extend(table_list["TableNames"])
        except KeyError:
            break
    if table_name_wildcard == "*":
        matching_tables = all_tables
    elif separator and table_name_wildcard.find(separator):
        matching_tables = list(set([table_name for table_names in [fnmatch.filter(all_tables, table_name) for table_name in table_name_wildcard.split(separator)] for table_name in table_names]))
    else:
        matching_tables = list(set(fnmatch.filter(all_tables, table_name_wildcard)))

    return matching_tables


def get_restore_table_matches(table_name_wildcard, separator):
    """
    Find tables to restore
    """

    # If backups are in S3 download and extract the backup to use during restoration
    if args.bucket:
        do_get_s3_archive(args.bucket, args.srcTable, args.archive, separator)
    matching_tables = []
    try:
        dir_list = [dir_name for dir_name in os.listdir(args.dumpPath) if os.path.isdir(args.dumpPath + "/" + dir_name)]
    except OSError:
        logging.info("Cannot find \"./%s\", Now trying current working directory..."
                     % args.dumpPath)
        dump_data_path = CURRENT_WORKING_DIR
        try:
            dir_list = [dir_name for dir_name in os.listdir(dump_data_path) if os.path.isdir(dump_data_path + "/" + dir_name)]
        except OSError:
            logging.info("Cannot find \"%s\" directory containing dump files!"
                         % dump_data_path)
            sys.exit(1)

    if table_name_wildcard == "*":
        matching_tables = dir_list
    elif separator and table_name_wildcard.find(separator):
        matching_tables = list(set([table_name for table_names in [fnmatch.filter(dir_list, table_name) for table_name in table_name_wildcard.split(separator)] for table_name in table_names]))
    else:
        matching_tables = [table_name_wildcard] if table_name_wildcard in dir_list else []

    return matching_tables


def change_prefix(source_table_name, source_wildcard, destination_wildcard, separator):
    """
    Update prefix used for searching tables
    """

    source_prefix = source_wildcard.split("*", 1)[0]
    destination_prefix = destination_wildcard.split("*", 1)[0]
    if separator == "":
        if re.sub(r"([A-Z])", r" \1", source_table_name).split()[0] == source_prefix:
            return destination_prefix + re.sub(r"([A-Z])", r" \1", source_table_name)\
                .split(" ", 1)[1].replace(" ", "")
    if source_table_name.split(separator, 1)[0] == source_prefix:
        return destination_prefix + separator + source_table_name.split(separator, 1)[1]


def delete_table(dynamo, sleep_interval, table_name):
    """
    Delete table table_name
    """

    if not args.dataOnly:
        while True:
            # delete table if exists
            table_exists = True
            try:
                dynamo.delete_table(TableName=table_name)
                logging.info(table_name + " deleting...")
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == "ResourceNotFoundException":
                    table_exists = False
                    logging.info(table_name + " not found for deletion!")
                    break
                elif e.response['Error']['Code'] == "LimitExceededException":
                    logging.info("Limit exceeded, retrying deletion of " + table_name + "...")
                    time.sleep(sleep_interval)
                elif e.response['Error']['Code'] == "ThrottlingException":
                    logging.info("Control plane limit exceeded, retrying deletion of " +
                                 table_name + "...")
                    time.sleep(sleep_interval)
                elif e.response['Error']['Code'] == "ResourceInUseException":
                    logging.info(table_name + " table is being deleted...")
                    time.sleep(sleep_interval)
                else:
                    logging.exception(e)
                    sys.exit(1)

        # if table exists, wait till deleted
        if table_exists:
            try:
                while True:
                    logging.info("Waiting for " + table_name + " table to be deleted... [" +
                                 dynamo.describe_table(TableName=table_name)["Table"]["TableStatus"] + "]")
                    time.sleep(sleep_interval)
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == "ResourceNotFoundException":
                    logging.info(table_name + " table deleted.")
                    pass
                else:
                    logging.exception(e)
                    sys.exit(1)


def mkdir_p(path):
    """
    Create directory to hold dump
    """

    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def batch_write(dynamo, sleep_interval, table_name, put_requests):
    """
    Write data to table_name
    """

    request_items = {table_name: put_requests}
    i = 1
    sleep = sleep_interval
    while True:
        response = dynamo.batch_write_item(RequestItems=request_items)
        unprocessed_items = response["UnprocessedItems"]

        if len(unprocessed_items) == 0:
            break
        if len(unprocessed_items) > 0 and i <= MAX_RETRY:
            logging.debug(str(len(unprocessed_items)) +
                          " unprocessed items, retrying after %s seconds... [%s/%s]"
                          % (str(sleep), str(i), str(MAX_RETRY)))
            request_items = unprocessed_items
            time.sleep(sleep)
            sleep += sleep_interval
            i += 1
        else:
            logging.info("Max retries reached, failed to processed batch write: " +
                         json.dumps(unprocessed_items, indent=JSON_INDENT))
            logging.info("Ignoring and continuing...")
            break


def wait_for_active_table(dynamo, table_name, verb=""):
    """
    Wait for table to be indesired state
    """

    while True:
        if dynamo.describe_table(TableName=table_name)["Table"]["TableStatus"] != "ACTIVE":
            logging.info("Waiting for " + table_name + " table to be " + verb + "... [" +
                         dynamo.describe_table(TableName=table_name)["Table"]["TableStatus"] + "]")
            time.sleep(sleep_interval)
        else:
            logging.info(table_name + " " + verb + ".")
            break


def update_provisioned_throughput(dynamo, table_name, read_capacity, write_capacity, wait=True):
    """
    Update provisioned throughput on the table to provided values
    """

    logging.info("Updating " + table_name + " table read capacity to: " +
                 str(read_capacity) + ", write capacity to: " + str(write_capacity))
    while True:
        try:
            dynamo.update_table(
                TableName=table_name,
                ProvisionedThroughput={
                    "ReadCapacityUnits": int(read_capacity),
                    "WriteCapacityUnits": int(write_capacity)
                }
            )
            break
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "LimitExceededException":
                logging.info("Limit exceeded, retrying updating throughput of " + table_name + "...")
                time.sleep(sleep_interval)
            elif e.response['Error']['Code'] == "ThrottlingException":
                logging.info("Control plane limit exceeded, retrying updating throughput"
                             "of " + table_name + "...")
                time.sleep(sleep_interval)

    # wait for provisioned throughput update completion
    if wait:
        wait_for_active_table(dynamo, table_name, "updated")


def do_empty(dynamo, table_name):
    """
    Empty table named table_name
    """

    logging.info("Starting Empty for " + table_name + "...")

    # get table schema
    logging.info("Fetching table schema for " + table_name)
    table_data = dynamo.describe_table(TableName=table_name)

    table_desc = table_data["Table"]
    original_read_capacity = table_desc["ProvisionedThroughput"]["ReadCapacityUnits"]
    original_write_capacity = table_desc["ProvisionedThroughput"]["WriteCapacityUnits"]
    table_args = {
        "AttributeDefinitions": table_desc["AttributeDefinitions"],
        "TableName": table_name,
        "KeySchema": table_desc["KeySchema"],
        "ProvisionedThroughput": {
            "ReadCapacityUnits": int(original_read_capacity),
            "WriteCapacityUnits": int(original_write_capacity)
        },
        "LocalSecondaryIndexes": table_desc.get("LocalSecondaryIndexes"),
        "GlobalSecondaryIndexes": table_desc.get("GlobalSecondaryIndexes")
    }
    logging.info("Deleting Table " + table_name)

    delete_table(dynamo, sleep_interval, table_name)

    logging.info("Creating Table " + table_name)
    while True:
        try:
            dynamo.create_table(**{k: v for k, v in table_args.items() if v})
            break
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "LimitExceededException":
                logging.info("Limit exceeded, retrying creation of " + table_name + "...")
                time.sleep(sleep_interval)
            elif e.response['Error']['Code'] == "ThrottlingException":
                logging.info("Control plane limit exceeded, retrying creation of " +
                             table_name + "...")
                time.sleep(sleep_interval)
            else:
                logging.exception(e)
                sys.exit(1)

    # wait for table creation completion
    wait_for_active_table(dynamo, table_name, "created")

    logging.info("Recreation of " + table_name + " completed. Time taken: " + str(
        datetime.datetime.now().replace(microsecond=0) - start_time))


def do_backup(dynamo, table_name, read_capacity, bucket=None):
    logging.info("Starting backup for " + table_name + "...")

    # trash data, re-create subdir
    if os.path.exists(args.dumpPath + os.sep + table_name):
        shutil.rmtree(args.dumpPath + os.sep + table_name)
    mkdir_p(args.dumpPath + os.sep + table_name)

    # get table schema
    logging.info("Dumping table schema for " + table_name)
    f = open(args.dumpPath + os.sep + table_name + os.sep + SCHEMA_FILE, "w+")
    table_desc = dynamo.describe_table(TableName=table_name)
    del table_desc['ResponseMetadata']
    if isinstance(table_desc.get('Table', {}).get('CreationDateTime', None), datetime.datetime):
        table_desc['Table']['CreationDateTime'] = time.mktime(table_desc['Table']['CreationDateTime'].timetuple())
    if table_desc.get('Table', {}).get('GlobalSecondaryIndexes', []):
        i = 0
        for index in table_desc['Table']['GlobalSecondaryIndexes']:
            if isinstance(index.get('ProvisionedThroughput', {}).get('LastDecreaseDateTime', None), datetime.datetime):
                table_desc['Table']['GlobalSecondaryIndexes'][i]['ProvisionedThroughput']['LastDecreaseDateTime'] = time.mktime(table_desc['Table']['GlobalSecondaryIndexes'][i]['ProvisionedThroughput']['LastDecreaseDateTime'].timetuple())
            if isinstance(index.get('ProvisionedThroughput', {}).get('LastIncreaseDateTime', None), datetime.datetime):
                table_desc['Table']['GlobalSecondaryIndexes'][i]['ProvisionedThroughput']['LastIncreaseDateTime'] = time.mktime(table_desc['Table']['GlobalSecondaryIndexes'][i]['ProvisionedThroughput']['LastIncreaseDateTime'].timetuple())
            i += 1
    if isinstance(table_desc.get('Table', {}).get('ProvisionedThroughput', {}).get('LastDecreaseDateTime', None), datetime.datetime):
        table_desc['Table']['ProvisionedThroughput']['LastDecreaseDateTime'] = time.mktime(table_desc['Table']['ProvisionedThroughput']['LastDecreaseDateTime'].timetuple())
    if isinstance(table_desc.get('Table', {}).get('ProvisionedThroughput', {}).get('LastIncreaseDateTime', None), datetime.datetime):
        table_desc['Table']['ProvisionedThroughput']['LastIncreaseDateTime'] = time.mktime(table_desc['Table']['ProvisionedThroughput']['LastIncreaseDateTime'].timetuple())

    f.write(json.dumps(table_desc, indent=JSON_INDENT))
    f.close()

    if not args.schemaOnly:
        original_read_capacity = table_desc["Table"]["ProvisionedThroughput"]["ReadCapacityUnits"]
        original_write_capacity = table_desc["Table"]["ProvisionedThroughput"]["WriteCapacityUnits"]

        # override table read capacity if specified
        if read_capacity and read_capacity != original_read_capacity:
            update_provisioned_throughput(dynamo, table_name, read_capacity, original_write_capacity)

        # get table data
        logging.info("Dumping table items for " + table_name)
        mkdir_p(args.dumpPath + os.sep + table_name + os.sep + DATA_DIR)

        i = 1
        last_evaluated_key = None
        scanned_table = dynamo.scan(TableName=table_name)
        while True:
            del scanned_table['ResponseMetadata']
            f = open(args.dumpPath + os.sep + table_name + os.sep + DATA_DIR + os.sep + str(i).zfill(4) + ".json", "w+")
            f.write(json.dumps(scanned_table, indent=JSON_INDENT))
            f.close()
            i += 1
            try:
                last_evaluated_key = scanned_table["LastEvaluatedKey"]
                scanned_table = dynamo.scan(TableName=table_name, ExclusiveStartKey=last_evaluated_key)
            except KeyError:
                break

        # revert back to original table read capacity if specified
        if read_capacity and read_capacity != original_read_capacity:
            update_provisioned_throughput(dynamo, table_name, original_read_capacity, original_write_capacity, False)

        logging.info("Backup for " + table_name + " table completed. Time taken: " + str(
            datetime.datetime.now().replace(microsecond=0) - start_time))
    if bucket:
        dump_path = args.dumpPath + os.sep + table_name
        did_archive, archive_file = do_archive(args.archive, dump_path)
        if did_archive:
            do_put_bucket_object(args.bucket, archive_file)


def do_restore(dynamo, sleep_interval, source_table, destination_table, write_capacity):
    """
    Restore table
    """
    logging.info("Starting restore for " + source_table + " to " + destination_table + "...")

    # create table using schema
    # restore source_table from dump directory if it exists else try current working directory
    if os.path.exists("%s/%s" % (args.dumpPath, source_table)):
        dump_data_path = args.dumpPath
    else:
        logging.info("Cannot find \"./%s/%s\", Now trying current working directory..."
                     % (args.dumpPath, source_table))
        if os.path.exists("%s/%s" % (CURRENT_WORKING_DIR, source_table)):
            dump_data_path = CURRENT_WORKING_DIR
        else:
            logging.info("Cannot find \"%s/%s\" directory containing dump files!"
                         % (CURRENT_WORKING_DIR, source_table))
            sys.exit(1)
    table_data = json.load(open(dump_data_path + os.sep + source_table + os.sep + SCHEMA_FILE))
    table = table_data["Table"]
    original_read_capacity = table["ProvisionedThroughput"]["ReadCapacityUnits"]
    original_write_capacity = table["ProvisionedThroughput"]["WriteCapacityUnits"]
    # table parameters for restore
    table_args = {
        "AttributeDefinitions": table["AttributeDefinitions"],
        "TableName": destination_table,
        "KeySchema": table["KeySchema"],
        "LocalSecondaryIndexes": table.get("LocalSecondaryIndexes"),
        "GlobalSecondaryIndexes": table.get("GlobalSecondaryIndexes")
    }

    # override table write capacity if specified, else use RESTORE_WRITE_CAPACITY if original
    # write capacity is lower
    if not write_capacity:
        if original_write_capacity < RESTORE_WRITE_CAPACITY:
            write_capacity = RESTORE_WRITE_CAPACITY
        else:
            write_capacity = original_write_capacity

    # override GSI write capacities if specified, else use RESTORE_WRITE_CAPACITY if original
    # write capacity is lower
    original_gsi_write_capacities = []
    table_global_secondary_indexes = table_args.get("GlobalSecondaryIndexes", None)
    if table_global_secondary_indexes:
        for gsi in table_global_secondary_indexes:
            original_gsi_write_capacities.append(gsi["ProvisionedThroughput"]["WriteCapacityUnits"])

            if gsi["ProvisionedThroughput"]["WriteCapacityUnits"] < int(write_capacity):
                gsi["ProvisionedThroughput"]["WriteCapacityUnits"] = int(write_capacity)

    # temp provisioned throughput for restore
    table_args["ProvisionedThroughput"] = {
        "ReadCapacityUnits": int(original_read_capacity),
        "WriteCapacityUnits": int(write_capacity)
    }
    if not args.dataOnly:
        logging.info("Creating " + destination_table + " table with temp write capacity of " +
                     str(write_capacity))
        while True:
            try:
                dynamo.create_table(**{k: v for k, v in table_args.items() if v})
                break
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == "LimitExceededException":
                    logging.info("Limit exceeded, retrying creation of " + destination_table + "...")
                    time.sleep(sleep_interval)
                elif e.response['Error']['Code'] == "ThrottlingException":
                    logging.info("Control plane limit exceeded, "
                                 "retrying creation of " + destination_table + "...")
                    time.sleep(sleep_interval)
                else:
                    logging.exception(e)
                    sys.exit(1)

        # wait for table creation completion
        wait_for_active_table(dynamo, destination_table, "created")
    else:
        # update provisioned capacity
        if int(write_capacity) > original_write_capacity:
            update_provisioned_throughput(dynamo,
                                          destination_table,
                                          original_read_capacity,
                                          write_capacity,
                                          False)

    if not args.schemaOnly:
        # read data files
        logging.info("Restoring data for " + destination_table + " table...")
        data_file_list = os.listdir(dump_data_path + os.sep + source_table +
                                    os.sep + DATA_DIR + os.sep)
        data_file_list.sort()

        for data_file in data_file_list:
            logging.info("Processing " + data_file + " of " + destination_table)
            items = []
            item_data = json.load(
                open(
                    dump_data_path + os.sep + source_table + os.sep + DATA_DIR + os.sep + data_file
                )
            )
            items.extend(item_data["Items"])

            # batch write data
            put_requests = []
            while len(items) > 0:
                put_requests.append({"PutRequest": {"Item": items.pop(0)}})

                # flush every MAX_BATCH_WRITE
                if len(put_requests) == MAX_BATCH_WRITE:
                    logging.debug("Writing next " + str(MAX_BATCH_WRITE) +
                                  " items to " + destination_table + "...")
                    batch_write(dynamo, BATCH_WRITE_SLEEP_INTERVAL, destination_table, put_requests)
                    del put_requests[:]

            # flush remainder
            if len(put_requests) > 0:
                batch_write(dynamo, BATCH_WRITE_SLEEP_INTERVAL, destination_table, put_requests)

        if not args.skipThroughputUpdate:
            # revert to original table write capacity if it has been modified
            if int(write_capacity) != original_write_capacity:
                update_provisioned_throughput(dynamo,
                                              destination_table,
                                              original_read_capacity,
                                              original_write_capacity,
                                              False)

            # loop through each GSI to check if it has changed and update if necessary
            if table_global_secondary_indexes:
                gsi_data = []
                for gsi in table_global_secondary_indexes:
                    wcu = gsi["ProvisionedThroughput"]["WriteCapacityUnits"]
                    rcu = gsi["ProvisionedThroughput"]["ReadCapacityUnits"]
                    original_gsi_write_capacity = original_gsi_write_capacities.pop(0)
                    if original_gsi_write_capacity != wcu:
                        gsi_data.append({
                            "Update": {
                                "IndexName": gsi["IndexName"],
                                "ProvisionedThroughput": {
                                    "ReadCapacityUnits":
                                        int(rcu),
                                    "WriteCapacityUnits": int(original_gsi_write_capacity)
                                }
                            }
                        })

                logging.info("Updating " + destination_table +
                             " global secondary indexes write capacities as necessary...")
                while True:
                    try:
                        dynamo.update_table(TableName=destination_table,
                                            GlobalSecondaryIndexUpdates=gsi_data)
                        break
                    except botocore.exceptions.ClientError as e:
                        if (e.response['Error']['Code'] == "LimitExceededException"):
                            logging.info(
                                "Limit exceeded, retrying updating throughput of"
                                "GlobalSecondaryIndexes in " + destination_table + "...")
                            time.sleep(sleep_interval)
                        elif (e.response['Error']['Code'] == "ThrottlingException"):
                            logging.info(
                                "Control plane limit exceeded, retrying updating throughput of"
                                "GlobalSecondaryIndexes in " + destination_table + "...")
                            time.sleep(sleep_interval)

        # wait for table to become active
        wait_for_active_table(dynamo, destination_table, "active")

        logging.info("Restore for " + source_table + " to " + destination_table +
                     " table completed. Time taken: " + str(
                         datetime.datetime.now().replace(microsecond=0) - start_time))
    else:
        logging.info("Empty schema of " + source_table + " table created. Time taken: " +
                     str(datetime.datetime.now().replace(microsecond=0) - start_time))


# parse args
def do_parse_args():
    """
        Here we parse arguments and return the populated argparse object
    """

    parser = argparse.ArgumentParser(description="Simple DynamoDB backup/restore/empty.")
    parser.add_argument("-a", "--archive", help="Type of compressed archive to create."
                        "If unset, don't create archive", choices=["zip", "tar"], default="zip")
    parser.add_argument("-b", "--bucket", help="S3 bucket in which to store or retrieve backups."
                        "[must already exist]")
    parser.add_argument("-m", "--mode", help="Operation to perform",
                        choices=["backup", "restore", "empty"])
    parser.add_argument("-r", "--region", help="AWS region to use, e.g. 'us-west-1'. "
                        "Can use AWS_DEFAULT_REGION for local testing.  Use '" +
                        LOCAL_REGION + "' for local DynamoDB testing")
    parser.add_argument("--host", help="Host of local DynamoDB [required only for local]")
    parser.add_argument("--port", help="Port of local DynamoDB [required only for local]")
    parser.add_argument("--accessKey", help="Access key of local DynamoDB "
                        "[required only for local]")
    parser.add_argument("--secretKey", help="Secret key of local DynamoDB "
                        "[required only for local]")
    parser.add_argument("-p", "--profile",
                        help="AWS credentials file profile to use. Allows you to use a "
                        "profile instead accessKey, secretKey authentication")
    parser.add_argument("-u", "--assumedAccountId",
                        help="Specify if the script should assume an iam role from another account")
    parser.add_argument("-n", "--assumedRoleName", help="Specify the role name to be asumed")
    parser.add_argument("--sessionToken", help="Pass an AWS session token from command line")
    parser.add_argument("-s", "--srcTable",
                        help="Source DynamoDB table name to backup or restore from, "
                        "use 'tablename*' for wildcard prefix selection or '*' for "
                        "all tables.  Mutually exclusive with --tag")
    parser.add_argument("-d", "--destTable",
                        help="Destination DynamoDB table name to backup or restore to, "
                        "use 'tablename*' for wildcard prefix selection "
                        "(defaults to use '-' separator) [optional, defaults to source]")
    parser.add_argument("--prefixSeparator", help="Specify a different prefix separator, "
                        "e.g. '.' [optional]")
    parser.add_argument("--noSeparator", action='store_true',
                        help="Overrides the use of a prefix separator for backup wildcard "
                        "searches [optional]")
    parser.add_argument("--readCapacity",
                        help="Change the temp read capacity of the DynamoDB table to backup "
                        "from [optional]")
    parser.add_argument("-t", "--tag", help="Tag to use for identifying tables to back up.  "
                        "Mutually exclusive with srcTable.  Provided as KEY=VALUE")
    parser.add_argument("--writeCapacity",
                        help="Change the temp write capacity of the DynamoDB table to restore "
                        "to [defaults to " + str(RESTORE_WRITE_CAPACITY) + ", optional]")
    parser.add_argument("--schemaOnly", action="store_true", default=False,
                        help="Backup or restore the schema only. Do not backup/restore data. "
                        "Can be used with both backup and restore modes. Cannot be used with "
                        "the --dataOnly [optional]")
    parser.add_argument("--dataOnly", action="store_true", default=False,
                        help="Restore data only. Do not delete/recreate schema [optional for "
                        "restore]")
    parser.add_argument("--skipThroughputUpdate", action="store_true", default=False,
                        help="Skip updating throughput values across tables [optional]")
    parser.add_argument("--dumpPath", help="Directory to place and search for DynamoDB table "
                        "backups (defaults to use '" + str(DATA_DUMP) + "') [optional]",
                        default=str(DATA_DUMP))
    parser.add_argument("--log", help="Logging level - DEBUG|INFO|WARNING|ERROR|CRITICAL "
                        "[optional]")
    return(parser.parse_args())


def main():
    """
    Entrypoint to the script
    """
    global args, sleep_interval, start_time
    args = do_parse_args()
    # set log level
    log_level = LOG_LEVEL
    if args.log is not None:
        log_level = args.log.upper()
    logging.basicConfig(level=getattr(logging, log_level), format=LOG_FORMAT)

    # Check to make sure that --dataOnly and --schemaOnly weren't simultaneously specified
    if args.schemaOnly and args.dataOnly:
        logging.error("Options --schemaOnly and --dataOnly are mutually exclusive.")
        sys.exit(1)

    # instantiate connection
    if args.region == LOCAL_REGION:
        dynamo = _get_aws_client("dynamodb", endpoint="http://%s:%s" % (args.host, args.port))
        sleep_interval = LOCAL_SLEEP_INTERVAL
    else:
        dynamo = _get_aws_client("dynamodb")
        sleep_interval = AWS_SLEEP_INTERVAL

    # don't proceed if connection is not established
    if not dynamo:
        logging.info("Unable to establish connection with dynamodb")
        sys.exit(1)

    # set prefix separator
    prefix_separator = DEFAULT_PREFIX_SEPARATOR
    if args.prefixSeparator is not None:
        prefix_separator = args.prefixSeparator
    if args.noSeparator is True:
        prefix_separator = None

    # do backup/restore
    start_time = datetime.datetime.now().replace(microsecond=0)
    if args.mode == "backup":
        if not args.srcTable:
            logging.info("No source table specified. Specify a table or list of tables to backup...")
            sys.exit(1)
        matching_backup_tables = get_table_name_matches(dynamo, args.srcTable, prefix_separator)
        if not matching_backup_tables:
            logging.info("No table found, exiting backup...")
            sys.exit(1)
        elif len(matching_backup_tables) > 1:
            logging.info("Found " + str(len(matching_backup_tables)) + " table(s) in DynamoDB to backup: " + ", ".join(matching_backup_tables))
            threads = []
            for table_name in matching_backup_tables:
                t = threading.Thread(target=do_backup, args=(dynamo, table_name, args.readCapacity, args.bucket,))
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)
            for thread in threads:
                thread.join()

            logging.info("Backup of tables " + ", ".join(matching_backup_tables) + " completed!")
        else:
            logging.info("Found " + matching_backup_tables[0] + " table in DynamoDB to backup")
            do_backup(dynamo, args.srcTable, args.readCapacity, args.bucket)

    elif args.mode == "restore":
        if not args.destTable:
            args.destTable = args.srcTable

        if not args.srcTable:
            logging.info("No source table specified. Specify a table or list of tables to restore...")
            sys.exit(1)

        matching_restore_tables = get_restore_table_matches(args.srcTable, prefix_separator)
        if len(matching_restore_tables) > 1:
            matching_destination_tables = get_table_name_matches(dynamo, prefix_separator.join(matching_restore_tables), prefix_separator)
        elif len(matching_restore_tables) == 1:
            matching_destination_tables = get_table_name_matches(dynamo, args.destTable, prefix_separator)

        if not matching_destination_tables:
            logging.info("No table destination table found for deletion, Going to restore...")
        elif len(matching_destination_tables) > 1:
            delete_str = ": " if args.dataOnly else " to be deleted: "
            logging.info(
                "Found " + str(len(matching_destination_tables)) +
                " table(s) in DynamoDB" + delete_str +
                ", ".join(matching_destination_tables))

            threads = []
            for table in matching_destination_tables:
                t = threading.Thread(target=delete_table, args=(dynamo, sleep_interval, table))
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()
        else:
            delete_table(dynamo, sleep_interval, matching_destination_tables[0])

        if not matching_restore_tables:
            logging.info("No table found for restore, exiting restore...")
            sys.exit(1)
        elif len(matching_restore_tables) > 1:
            logging.info(
                "Found " + str(len(matching_restore_tables)) +
                " table(s) in " + args.dumpPath + " to restore: " + ", ".join(
                    matching_restore_tables))

            threads = []
            for source_table in matching_restore_tables:
                t = threading.Thread(target=do_restore,
                                     args=(dynamo,
                                           sleep_interval,
                                           source_table,
                                           source_table,
                                           args.writeCapacity))
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()

            logging.info("Restore of tables " + ", ".join(matching_restore_tables) + " completed!")
        else:
            delete_table(dynamo, sleep_interval, args.destTable)
            do_restore(dynamo, sleep_interval, args.srcTable, args.destTable, args.writeCapacity)
            logging.info("Restore of table " + args.srcTable + " to " +
                         args.destTable + " completed!")

    elif args.mode == "empty":
        matching_tables = get_table_name_matches(dynamo, args.srcTable, prefix_separator)
        if not matching_tables:
            logging.info("No table found, exiting emptying process...")
            sys.exit(1)
        elif len(matching_tables) > 1:
            logging.info("Found " + str(len(matching_tables)) +
                         " table(s) in DynamoDB to empty: " +
                         ", ".join(matching_tables))

            threads = []
            for table in matching_tables:
                t = threading.Thread(target=do_empty, args=(dynamo, table))
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()

            logging.info("Empty of table(s) " + ", ".join(matching_tables) + " completed!")
        else:
            logging.info("Found " + matching_tables[0] + " table in DynamoDB to empty")
            do_empty(dynamo, matching_tables[0])


if __name__ == "__main__":
    main()

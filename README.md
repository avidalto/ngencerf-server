# Create virtual environment and install dependencies

Connect to the root directory where you cloned the server repo, assumed to be `$cerfServer`

**_Important:_**
Make sure you create the virtual environment with Python 3.11.
You might have to use the `python3.11` command instead of `python`
Once you are in the virtual environment, you can use `python`

```
$ cd $cerfServer
$ python3.11 -m venv .venv-cerf
$ source $cerfServer/.venv-cerf/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

# Setup local configuration
There are 2 files which need to be copied in order to provide custom settings for this installation.
The `settings.py` file contains settings that are applicable to all environments and should normally not be changed.

You should make copies of `__local_settings.py` and `__.env`. 
```
cp $cerfServer/cerfServer/__local_settings.py cerfServer/local_settings.py
cp $cerfServer/cerfServer/__.env cerfServer/.env
```
The 2 template files are suitable for development and no changes need to be made.
Note that these files are not checked in to Git

# Install Redis
Redis is used for the cache

```
sudo apt update
sudo apt install redis-server -y
```
Copy `./redis/redis.conf.dev` to `/etc/redis/redis.conf` and then start the service
```
sudo cp ./redis/redis.conf.dev /etc/redis/redis.conf

sudo systemctl enable redis-server
sudo systemctl start redis-server

redis-cli ping
```

# Create data directory

Create a directory that will hold the data.  It can be anything, such as `~/ngwpc/data`.  But a symbolic link needs to be created to match the location in the ngen/cal-mgr Docker, 
which is `/ngencerf/data`.
This is defined in `settings.py` as the mount point.

Enter these commands to create the top-level `/ngencerf` directory and then create the symbolic link

```
sudo mkdir /ngencerf
sudo ln -s ~/ngwpc/data /ngencerf/data
```


# Access to AWS
This needs to be done if you are running on AWS Workspace

Some endpoints require access to AWS and therefore you must update your credentials.
The credentials only last a few hours, so be prepared to refresh them at least once a day.
Follow instructions here: https://confluence.nextgenwaterprediction.com/display/NGWPC/Accessing+S3+Bucket+Programmatically+or+through+AWS+CLI, 
to get your credentials.
Add them to your `~/.aws/credentials` file (create the file if it doesn't exist)
You should manually add the region.  The file will look something like this

```
[default]
region=us-east-1

aws_access_key_id = <key_id>
aws_secret_access_key = <access_key>
aws_session_token = <token>
```

# Archive Directory

In `ngencerf/.env`, Define an s3 bucket/directory that will be used for archiving.

In AWS Workspace, you can use any directory that you have write access to.  For example,
```
`NGENCERF_ARCHIVE_S3_PATH=s3://ngwpc-dev/peter.kronenberg/ngencerf_archive/`
 ```
Use your own directory. Do not sure a directory with someone else

For Parallel Works, you must use the directory corresponding to the cluster and for which you have read/write access.
The bucket used is `s3://ngwpc-ngencerf-archive` and the directory will be unique for each cluster, e.g., `s3://ngwpc-ngencerf-archive/integration`
```
`NGENCERF_ARCHIVE_S3_PATH=s3://ngwpc-ngencerf-archive/integration
```

**_Important:_**
Since S3 directories aren't real directories, they will not persist if they are empty.  So it is important to
create a dummy file in the directory that will remain there.  Enter this command
```
printf "Do not delete.\nThis placeholder file ensures this S3 prefix is retained.\nS3 does not preserve empty directories; at least one object must exist.\n" \
  | aws s3 cp - s3://ngwpc-dev/peter.kronenberg/ngencerf_archive/.keep
```
or
```
printf "Do not delete.\nThis placeholder file ensures this S3 prefix is retained.\nS3 does not preserve empty directories; at least one object must exist.\n" \
  | aws s3 cp - s3://ngwpc-ngencerf-archive/integration/.keep
```

# Static Files
There are some static files that are required for Ngen to run.  They should be in a directory under the data directory at `/ngencerf/data` called `ngen-static-files`.  

The data for the `ngen-static-files` directory is in 2 locations.  Copy everything from  `s3://ngwpc-dev/ngen-static-files/` to
`/ngencerf/data/ngen-static-files` or `/ngencerf-app/data/ngen-cal-data/ngen-static-files`
```
aws s3 cp --recursive s3://ngwpc-dev/ngen-static-files /ngencerf/data/ngen-static-files
```

In addition, copy the directory `module_parameter_files` and all its contents from 
https://github.com/NGWPC/nwm-msw-mgr/tree/development/src/mswm/module_parameter_files to the `/ngencerf/data/ngen-static-files` directory.

```
cd /ngencerf/data/ngen-static-files (for PW, use /ngencerf-app/data/ngen-cal-data/ngen-static-files)
rm -rf module_parameter_files
git clone --depth 1 --filter=blob:none --sparse -b development https://github.com/NGWPC/nwm-msw-mgr.git tmp-nwm-msw-mgr && \
cd tmp-nwm-msw-mgr && \
git sparse-checkout set src/mswm/module_parameter_files && \
mv src/mswm/module_parameter_files ../ && \
cd .. && rm -rf tmp-nwm-msw-mgr
```

Copy the directory `https://github.com/NGWPC/ngen-forcing/tree/development/NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates` 
and all its contents to the `/ngencerf/data/ngen-static-files` directory as `bmi_forcing_templates`

```
cd /ngencerf/data/ngen-static-files (for PW, use /ngencerf-app/data/ngen-cal-data/ngen-static-files)
rm -rf bmi_forcing_templates
git clone --depth 1 --filter=blob:none --sparse -b development https://github.com/NGWPC/ngen-forcing.git tmp-ngen-forcing && \
cd tmp-ngen-forcing && \
git sparse-checkout set NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates && \
mv NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates ../bmi_forcing_templates && \
cd .. && rm -rf tmp-ngen-forcing
```

From the directory `https://github.com/NGWPC/nwm-verf/tree/development/data/inputs`, 
copy only the *.parquet files to the `/ngencerf/data/ngen-static-files/verfication_data` directory

```
cd /ngencerf/data/ngen-static-files (for PW, use /ngencerf-app/data/ngen-cal-data/ngen-static-files)
rm -rf verification_data
git clone --depth 1 --filter=blob:none --sparse -b development https://github.com/NGWPC/nwm-verf.git tmp-ngen-verf && \
cd tmp-ngen-verf && \
git sparse-checkout set data/inputs && \
mkdir -p ../verification_data && \
find data/inputs -type f -name '*.parquet' -exec cp {} ../verification_data/ \; && \
cd .. && rm -rf tmp-ngen-verf
```

When done, your `ngen-static-files` directory should look something like this


ngen-static-files/
├── bmi_forcing_templates
├── module_parameter_files
│  ├── lasam
│  ├── noah-owp-modular
│  └── ueb
├── nwm_retrospective
└── parquet



# Initial Set-up of database

Install Postgres if not already installed.
```
sudo apt update
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb\_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
wget -qO- https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo tee /etc/apt/trusted.gpg.d/pgdg.asc &>/dev/null
sudo apt install postgresql postgresql-client -y
systemctl status postgresql
```

Change the password for the Admin user
```
sudo -u postgres psql
ALTER USER postgres PASSWORD 'postgres';
\q
```

Confirm that you can log in with the new password
```
# psql -h localhost -U postgres
```


**_Important:_**
During development, there might be times when the entire database needs to be initialized.  
In that case, drop all existing tables in the database and run these initialize steps again.

To drop all tables, you can use this script:
```
do $$ declare
    r record;
begin
    for r in (select tablename from pg_tables where schemaname = 'public') loop
        execute 'drop table if exists ' || quote_ident(r.tablename) || ' cascade';
    end loop;
end $$;
```
where `public` is the name of your schema.


# Running the server
To run the server, use `runCerf.sh`. 
```
./runCerf.sh
```

**Note:** If running with NGEN_ENVIRONMENT=LOCAL or DOCKER, then it is important to run `pre_start.py` from `manage.py` before the
server starts in order to clean up any Calibrations or Validations that were running at the time the server went down.
This is not necessary when running on Parallel Works


# User Authentication

All endpoints require a user to be authenticated.  You can create a user through the front-end UI, the command-line interface or use this curl command:

You can use this `curl` command
```
curl --location 'localhost:8000/auth/users/' \
--header 'Content-Type: application/json' \
--data-raw '{
    "email": "<email>",
    "password": "<password>"
}'
```

To use the CLI, from the `cli` directory, enter
`./ngencerf register`

User creation only needs to be done once.

To simulate a login, send the same payload, containing the email and password, to the endpoint `auth/awt/create`

Extract the access token.  For all subsequent requests, you need to include an `Authorization` header of 
type `Bearer token` that includes the access token.

# Importing test data

```
Note: Need to update to reference to new CLI
```

The `cli` directory contains an `ngencerf.sh` command line script which will allow you to import data and create a calibration run job without having to go though the UI.  

In the `import_test_data` directory, there are some sample import data files.  Set environment variables with your email and password (or put them in ~/.bashrc)
```
$ export NGEN_EMAIL="your_email"
$ export NGEN_PASSWORD="your_password"
```

You can then run the `ngencerf.sh` script with one of the sample input files.  Everytime you run `ngencerf.sh`, a new Calibration Run job will be created.  
The error messages that you get from the import are intended to let you know which data is still required to make the job runnable and at this point, can be ignored.

The metadata section is totally ignored on import and can be used to add your own comments, as long as it is in Json format.

See [NgenCERF Command Line Interface (CLI)](https://confluence.nextgenwaterprediction.com/pages/viewpage.action?pageId=20056845)

# Runtime environments

There are 3 environments that ngen/ngen-cerf can run in, defined by `settings.NGEN_ENVIRONMENT` in .env

```
NGEN_ENVIRONMENT = DOCKER
```


1. LOCAL - ngen and cal-mgr, as well as ngen-fcst and ngen-forcing, must be installed on your local machine, for example, in `~/noaa-owp/ngen` and `~/noaa-owp/cal-mgr`
Create a symbolic link to match the specifying in settings.py.
All the repos should be installed in the same directory.  It can be anything, but a symbolic link needs to be created to match the location in the Docker containers, 
which is `/ngen-app`.
   ```
   sudo mkdir /ngen-app
   sudo ln -s ~/noaa-owp /ngen-app
   ```
   This environment is the hardest to set up because of the steps involved in installing ngen and cal-mgr, and is not recommended.


2. DOCKER - ngen and cal-mgr are installed in a docker container.  This is the easiest for running locally.
Follow these steps to pull the latest docker containers. 

   1. If you don't have Docker installed, follow the instructions here: https://confluence.nextgenwaterprediction.com/display/NGWPC/AWS+Ubuntu+22.04+LTS+Workspace+for+Docker#AWSUbuntu22.04LTSWorkspaceforDocker-InstallDocker
   2. Follow the instructions here to 'Manage Docker as a non-root user': https://docs.docker.com/engine/install/linux-postinstall/#manage-docker-as-a-non-root-user
   3. (Use your AWS credentials to login)
   ```

   docker pull ghcr.io/ngwpc/nwm-cal-mgr:latest && docker tag ghcr.io/ngwpc/nwm-cal-mgr nwm-cal-mgr
   docker pull ghcr.io/ngwpc/nwm-fcst-mgr:latest && docker tag ghcr.io/ngwpc/nwm-fcst-mgr:latest nwm-fcst-mgr
   docker pull ghcr.io/ngwpc/ngen-bmi-forcing:latest && docker tag ghcr.io/ngwpc/ngen-bmi-forcing:latest ngen-bmi-forcing
   ```

   **Note:** If you are developing and have updates to the repos that you want to include, use one of the following from the appropriate repo directory:
   ```
  docker build --tag=nwm-cal-mgr . 
  docker build --tag=nwm-fcst-mgr . 
  docker build --file Dockerfile.bmi-forcings --tag=ngen-bmi-forcing . 
   ```
 
3. PARALLEL_WORKS - The dockers containers are built for you and the server uses Slurm to communicate.



# Directory structure

By convention with the Docker images, the mount point is at `/ngencerf/data`.   This is defined in `settings.py` and should not change without proper coordination. 

`/ngencerf/data` contains `ngen-static-files` and `ngen-cal-work`

`ngen-cal-work/run_calib` contains the data for ngen and cal-mgr

Files from Data Services are in `s3/ngwpc-dev/hyrofabric`.  This is an S3 bucket that is mounted as a file system.  This allows us not to have to worry about downloading files from S3. 
This is a shared location, since these files can be re-used by different jobs for the same gage.

Prior to running the job, the Observation and Forcing files from Data Services will be subsetted to conform to the time range of the job.
These files will be placed in the instance specific directory, as described above.


```
peter.a.kronenberg@U-12SMBYD5450YI:~$ tree /ngencerf -L 4 -n -A
/ngencerf
└── data
    ├── ngen-cal-work
    │   ├── run_calib
    │   │   ├── 100_peter
    │   │   │    ├── forcing
    │   │   │    ├── observation
    │   │   │    ├── geopackage
    │   │   │    ├── parameters.txt
    │   │   │    └── sloth_parameters.txt
    │   │   └── 98_peter
    │   │        ├── forcing
    │   │        ├── observation
    │   │        ├── geopackage
    │   │        ├── parameters.txt
    │   │        └── sloth_parameters.txt
    │   └── venv.cal
    └── ngen-static-files
        ├── bmi_config
        │   └── Noah-OWP
        │       ├── GENPARM.TBL
        │       ├── MPTABLE.TBL
        │       └── SOILPARM.TBL
        ├── parquet
        │   └── conus_model_attributes.parquet
        └── nwm_retrospective
            ├── 01118000.csv
            ├── 01121000.csv
            ├── 01123000.csv
            ├── 01127500.csv
            ├── 01130000.csv
            └── 01134500.csv

.
└── s3
    └── ngwpc-dev/hydrfabric  
```


# Installing ngen and nwm-cal-mgr
**Note:** This process is not recommended.  Run ngen and nwm-cal-mgr in a docker container as described in Runtime Environments

Follow the instructions at https://confluence.nextgenwaterprediction.com/display/NGWPC/Build+ngen-cal+and+ngen+from+GitLab. 

Use these recommended directory names to avoid having to change your settings.
* It is recommended that you create a directory called `~/ngwpc/data/ngen-cal-work`
* It is recommended that you clone ngen and cal-mgr in a directory called `~/noaa-owp/ngen` and `~/noaa-owp/cal-mgr`


* Create the cal-mgr virtual environment.  This directory is defined in `settings.py` as `NGEN_CAL_VENV`.   Default location is `~/ngen-cal-work/venv-cal`
* Clone cal-mgr from Gitlab.  This directory is defined in `settings.py` as `CAL_MGR__REPO_ROOT`.  Default location is `~/noaa-owp/cal-mgr`
* Follow instructions for installing cal-mgr
* Clone ngen from Gitlab into `~/noaa-owp/ngen`
* Follow instructions for installing ngen
* It is **not** necessary to create the ROOT_DIR_RUN_NGEN_CAL directory or to run the script that creates symbolic links in that directory
* Create the `NGEN_CAL_RUN_DIR` at `~/ngwpc/data/run_calib`


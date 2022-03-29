"""
Actions available when deploying many JupyterHubs to many Kubernetes clusters
"""
import os
import shutil
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from auth import KeyProvider
from cluster import Cluster
from utils import print_colour
from file_acquisition import find_absolute_path_to_cluster_file, get_decrypted_file
from config_validation import (
    validate_cluster_config,
    validate_hub_config,
    validate_support_config,
    assert_single_auth_method_enabled,
)
from helm_upgrade_decision import (
    assign_staging_jobs_for_missing_clusters,
    discover_modified_common_files,
    ensure_support_staging_jobs_have_correct_keys,
    get_all_cluster_yaml_files,
    generate_hub_matrix_jobs,
    generate_support_matrix_jobs,
    move_staging_hubs_to_staging_matrix,
    pretty_print_matrix_jobs,
)


# Without `pure=True`, I get an exception about str / byte issues
yaml = YAML(typ="safe", pure=True)
helm_charts_dir = Path(__file__).parent.parent.joinpath("helm-charts")


def use_cluster_credentials(cluster_name):
    """
    Quickly gain command-line access to a cluster by updating the current
    kubeconfig file to include the deployer's access credentials for the named
    cluster and mark it as the cluster to work against by default.

    This function is to be used with the `use-cluster-credentials` CLI
    command only - it is not used by the rest of the deployer codebase.
    """
    validate_cluster_config(cluster_name)

    config_file_path = find_absolute_path_to_cluster_file(cluster_name)
    with open(config_file_path) as f:
        cluster = Cluster(yaml.load(f), config_file_path.parent)

    # Cluster.auth() method has the context manager decorator so cannot call
    # it like a normal function
    with cluster.auth():
        # This command will spawn a new shell with all the env vars (including
        # KUBECONFIG) inherited, and once you quit that shell the python program
        # will resume as usual.
        # TODO: Figure out how to change the PS1 env var of the spawned shell
        # to change the prompt to f"cluster-{cluster.spec['name']}". This will
        # make it visually clear that the user is now operating in a different
        # shell.
        subprocess.check_call([os.environ["SHELL"], "-l"])


def deploy_support(cluster_name):
    """
    Deploy support components to a cluster
    """
    validate_cluster_config(cluster_name)
    validate_support_config(cluster_name)

    config_file_path = find_absolute_path_to_cluster_file(cluster_name)
    with open(config_file_path) as f:
        cluster = Cluster(yaml.load(f), config_file_path.parent)

    if cluster.support:
        with cluster.auth():
            cluster.deploy_support()


def deploy_grafana_dashboards(cluster_name):
    """
    Deploy grafana dashboards to a cluster that provide useful metrics
    for operating a JupyterHub

    Grafana dashboards and deployment mechanism in question are maintained in
    this repo: https://github.com/jupyterhub/grafana-dashboards
    """
    validate_cluster_config(cluster_name)
    validate_support_config(cluster_name)

    config_file_path = find_absolute_path_to_cluster_file(cluster_name)
    with open(config_file_path) as f:
        cluster = Cluster(yaml.load(f), config_file_path.parent)

    # If grafana support chart is not deployed, then there's nothing to do
    if not cluster.support:
        print_colour(
            "Support chart has not been deployed. Skipping Grafana dashboards deployment..."
        )
        return

    grafana_token_file = (config_file_path.parent).joinpath(
        "enc-grafana-token.secret.yaml"
    )

    # Read the cluster specific secret grafana token file
    with get_decrypted_file(grafana_token_file) as decrypted_file_path:
        with open(decrypted_file_path) as f:
            config = yaml.load(f)

    # Check GRAFANA_TOKEN exists in the secret config file before continuing
    if "grafana_token" not in config.keys():
        raise ValueError(
            f"`grafana_token` not provided in secret file! Please add it and try again: {grafana_token_file}"
        )

    # FIXME: We assume grafana_url and uses_tls config will be defined in the first
    #        file listed under support.helm_chart_values_files.
    support_values_file = cluster.support.get("helm_chart_values_files", [])[0]
    with open(config_file_path.parent.joinpath(support_values_file)) as f:
        support_values_config = yaml.load(f)

    # Get the url where grafana is running from the support values file
    grafana_url = (
        support_values_config.get("grafana", {}).get("ingress", {}).get("hosts", {})
    )
    uses_tls = (
        support_values_config.get("grafana", {}).get("ingress", {}).get("tls", {})
    )

    if not grafana_url:
        print_colour(
            "Couldn't find `config.grafana.ingress.hosts`. Skipping Grafana dashboards deployment..."
        )
        return

    grafana_url = (
        f"https://{grafana_url[0]}" if uses_tls else f"http://{grafana_url[0]}"
    )

    # Use the jupyterhub/grafana-dashboards deployer to deploy the dashboards to this cluster's grafana
    print_colour("Cloning jupyterhub/grafana-dashboards...")

    dashboards_dir = "grafana_dashboards"

    subprocess.check_call(
        [
            "git",
            "clone",
            "https://github.com/jupyterhub/grafana-dashboards",
            dashboards_dir,
        ]
    )

    # We need the existing env too for the deployer to be able to find jssonnet and grafonnet
    deploy_env = os.environ.copy()
    deploy_env.update({"GRAFANA_TOKEN": config["grafana_token"]})

    try:
        print_colour(f"Deploying grafana dashboards to {cluster_name}...")
        subprocess.check_call(
            ["./deploy.py", grafana_url], env=deploy_env, cwd=dashboards_dir
        )

        print_colour(f"Done! Dashboards deployed to {grafana_url}.")
    finally:
        # Delete the directory where we cloned the repo.
        # The deployer cannot call jsonnet to deploy the dashboards if using a temp directory here.
        # Might be because opening more than once of a temp file is tried
        # (https://docs.python.org/3.8/library/tempfile.html#tempfile.NamedTemporaryFile)
        shutil.rmtree(dashboards_dir)


def deploy(cluster_name, hub_name, skip_hub_health_test, config_path):
    """
    Deploy one or more hubs in a given cluster
    """
    validate_cluster_config(cluster_name)
    validate_hub_config(cluster_name, hub_name)
    assert_single_auth_method_enabled(cluster_name, hub_name)

    with get_decrypted_file(config_path) as decrypted_file_path:
        with open(decrypted_file_path) as f:
            config = yaml.load(f)

    # Most of our hubs use Auth0 for Authentication. This lets us programmatically
    # determine what auth provider each hub uses - GitHub, Google, etc. Without
    # this, we'd have to manually generate credentials for each hub - and we
    # don't want to do that. Auth0 domains are tied to a account, and
    # this is our auth0 domain for the paid account that 2i2c has.
    auth0 = config["auth0"]

    k = KeyProvider(auth0["domain"], auth0["client_id"], auth0["client_secret"])

    # Each hub needs a unique proxy.secretToken. However, we don't want
    # to manually generate & save it. We also don't want it to change with
    # each deploy - that causes a pod restart with downtime. So instead,
    # we generate it based on a single secret key (`PROXY_SECRET_KEY`)
    # combined with the name of each hub. This way, we get unique,
    # cryptographically secure proxy.secretTokens without having to
    # keep much state. We can rotate them by changing `PROXY_SECRET_KEY`.
    # However, if `PROXY_SECRET_KEY` leaks, that means all the hub's
    # proxy.secretTokens have leaked. So let's be careful with that!
    SECRET_KEY = bytes.fromhex(config["secret_key"])

    config_file_path = find_absolute_path_to_cluster_file(cluster_name)
    with open(config_file_path) as f:
        cluster = Cluster(yaml.load(f), config_file_path.parent)

    with cluster.auth():
        hubs = cluster.hubs
        if hub_name:
            hub = next((hub for hub in hubs if hub.spec["name"] == hub_name), None)
            print_colour(f"Deploying hub {hub.spec['name']}...")
            hub.deploy(k, SECRET_KEY, skip_hub_health_test)
        else:
            for i, hub in enumerate(hubs):
                print_colour(
                    f"{i+1} / {len(hubs)}: Deploying hub {hub.spec['name']}..."
                )
                hub.deploy(k, SECRET_KEY, skip_hub_health_test)


def generate_helm_upgrade_jobs(changed_filepaths, pretty_print=False):
    """Analyse added or modified files from a GitHub Pull Request and decide which
    clusters and/or hubs require helm upgrades to be performed for their *hub helm
    charts or the support helm chart.

    Args:
        changed_filepaths (list[str]): A list of files that have been added or
            modified by a GitHub Pull Request
        pretty_print (bool, optional): If True, output a human readable table of jobs
            to be run using rich. If False, output a list of dictionaries to be
            passed to a GitHub Actions matrix job. Defaults to False.
    """
    (
        upgrade_support_on_all_clusters,
        upgrade_all_hubs_on_all_clusters,
    ) = discover_modified_common_files(changed_filepaths)

    # Get a list of filepaths to all cluster.yaml files in the repo
    cluster_files = get_all_cluster_yaml_files()

    # Empty lists to store job definitions in
    prod_hub_matrix_jobs = []
    support_and_staging_matrix_jobs = []

    for cluster_file in cluster_files:
        # Read in the cluster.yaml file
        with open(cluster_file) as f:
            cluster_config = yaml.load(f)

        # Get cluster's name and its cloud provider
        cluster_name = cluster_config.get("name", {})
        provider = cluster_config.get("provider", {})

        # Generate template dictionary for all jobs associated with this cluster
        cluster_info = {
            "cluster_name": cluster_name,
            "provider": provider,
            "reason_for_redeploy": "",
        }

        # Check if this cluster file has been modified. If so, set boolean flags to True
        intersection = set(changed_filepaths).intersection([str(cluster_file)])
        if intersection:
            print(
                f"This cluster.yaml file has been modified. Generating jobs to upgrade all hubs and the support chart on THIS cluster: {cluster_name}"
            )
            upgrade_all_hubs_on_this_cluster = True
            upgrade_support_on_this_cluster = True
            cluster_info["reason_for_redeploy"] = "cluster.yaml file was modified"
        else:
            upgrade_all_hubs_on_this_cluster = False
            upgrade_support_on_this_cluster = False

        # Generate a job matrix of all hubs that need upgrading on this cluster
        prod_hub_matrix_jobs.extend(
            generate_hub_matrix_jobs(
                cluster_file,
                cluster_config,
                cluster_info,
                set(changed_filepaths),
                upgrade_all_hubs_on_this_cluster=upgrade_all_hubs_on_this_cluster,
                upgrade_all_hubs_on_all_clusters=upgrade_all_hubs_on_all_clusters,
            )
        )

        # Generate a job matrix for support chart upgrades
        support_and_staging_matrix_jobs.extend(
            generate_support_matrix_jobs(
                cluster_file,
                cluster_config,
                cluster_info,
                set(changed_filepaths),
                upgrade_support_on_this_cluster=upgrade_support_on_this_cluster,
                upgrade_support_on_all_clusters=upgrade_support_on_all_clusters,
            )
        )

    # Clean up the matrix jobs
    (
        prod_hub_matrix_jobs,
        support_and_staging_matrix_jobs,
    ) = move_staging_hubs_to_staging_matrix(
        prod_hub_matrix_jobs, support_and_staging_matrix_jobs
    )
    support_and_staging_matrix_jobs = ensure_support_staging_jobs_have_correct_keys(
        support_and_staging_matrix_jobs, prod_hub_matrix_jobs
    )
    support_and_staging_matrix_jobs = assign_staging_jobs_for_missing_clusters(
        support_and_staging_matrix_jobs, prod_hub_matrix_jobs
    )

    # The existence of the CI environment variable is an indication that we are running
    # in an GitHub Actions workflow
    # https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#example-defining-outputs-for-a-job
    # We should always default to pretty printing the results of the decision logic
    # if we are not running in GitHub Actions, even when the --pretty-print flag has
    # not been parsed on the command line. This will avoid errors trying to set CI
    # output variables in an environment that doesn't exist.
    ci_env = os.environ.get("CI", False)
    if pretty_print or not ci_env:
        pretty_print_matrix_jobs(prod_hub_matrix_jobs, support_and_staging_matrix_jobs)
    else:
        # Add these matrix jobs as output variables for use in another job
        # https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions
        print(f"::set-output name=prod-hub-matrix-jobs::{prod_hub_matrix_jobs}")
        print(
            f"::set-output name=support-and-staging-matrix-jobs::{support_and_staging_matrix_jobs}"
        )

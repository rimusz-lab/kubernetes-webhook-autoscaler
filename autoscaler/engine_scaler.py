from azure.cli.core.commands.client_factory import get_mgmt_service_client
from azure.mgmt.resource.resources import ResourceManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.storage.blob import BlockBlobService
import time
import logging
import autoscaler.utils as utils
from autoscaler.agent_pool import AgentPool
from autoscaler.scaler import Scaler, ClusterNodeState
from threading import Thread, Lock

logger = logging.getLogger(__name__)


class EngineScaler(Scaler):

    def __init__(
            self, resource_group, nodes,
            over_provision, spare_count, dry_run,
            deployments, arm_template, arm_parameters):

        Scaler.__init__(
            self, resource_group, nodes, over_provision,
            spare_count, dry_run, deployments)

        self.arm_parameters = arm_parameters
        self.arm_template = arm_template

    def scale_down(self, trim_map, dry_run):
        """
        Scale down each agent pool (most recent nodes will be deleted first)
        """
        new_pool_sizes = {}
        for pool in self.agent_pools:
            new_agent_count = pool.actual_capacity - trim_map[pool.name]
            if new_agent_count <= 0:
                raise Exception(
                    "Tried to scale down pool {} to less than 1 agent".format(pool.name))

            logger.info("Scaling down pool {} by {} agents".format(
                pool.name, trim_map[pool.name]))
            new_pool_sizes[pool.name] = new_agent_count

        self.scale_pools(new_pool_sizes, dry_run, False)

    def delete_resources_for_node(self, node):
        logger.info('deleting node {}'.format(node.name))
        resource_management_client = get_mgmt_service_client(
            ResourceManagementClient)
        compute_management_client = get_mgmt_service_client(
            ComputeManagementClient)

        # save disk location
        vm_details = compute_management_client.virtual_machines.get(
            self.resource_group_name, node.name, None)
        storage_infos = vm_details.storage_profile.os_disk.vhd.uri.split('/')
        account_name = storage_infos[2].split('.')[0]
        container_name = storage_infos[3]
        blob_name = storage_infos[4]

        # delete vm
        logger.info('Deleting VM for {}'.format(node.name))
        delete_vm_op = resource_management_client.resources.delete(self.resource_group_name,
                                                                   'Microsoft.Compute',
                                                                   '',
                                                                   'virtualMachines',
                                                                   node.name,
                                                                   '2016-03-30')
        delete_vm_op.wait()

        # delete nic
        logger.info('Deleting NIC for {}'.format(node.name))
        name_parts = node.name.split('-')
        nic_name = '{}-{}-{}-nic-{}'.format(
            name_parts[0], name_parts[1], name_parts[2], name_parts[3])
        delete_nic_op = resource_management_client.resources.delete(self.resource_group_name,
                                                                    'Microsoft.Network',
                                                                    '',
                                                                    'networkInterfaces',
                                                                    nic_name,
                                                                    '2016-03-30')
        delete_nic_op.wait()

        # delete os blob
        logger.info('Deleting OS disk for {}'.format(node.name))
        storage_management_client = get_mgmt_service_client(
            StorageManagementClient)
        keys = storage_management_client.storage_accounts.list_keys(
            self.resource_group_name, account_name)
        key = keys.keys[0].value

        block_blob_service = BlockBlobService(
            account_name=account_name, account_key=key)
        block_blob_service.delete_blob(container_name, blob_name)

    def delete_node(self, pool, node, lock):
        pool_sizes = {}
        with lock:
            for pool in self.agent_pools:
                pool_sizes[pool.name] = pool.actual_capacity
            pool_sizes[pool.name] = pool.actual_capacity - 1
            self.deployments.requested_pool_sizes = pool_sizes

        self.delete_resources_for_node(node)

    def scale_pools(self, new_pool_sizes, is_scale_up):
        has_changes = False
        for pool in self.agent_pools:
            new_size = new_pool_sizes[pool.name]
            new_pool_sizes[pool.name] = min(pool.max_size, new_size)
            if new_pool_sizes[pool.name] == pool.actual_capacity:
                logger.info("Pool '{}' already at desired capacity ({})".format(
                    pool.name, pool.actual_capacity))
                continue
            has_changes = True

            if not self.dry_run:
                if new_size > pool.actual_capacity:
                    pool.reclaim_unschedulable_nodes(new_size)
            else:
                logger.info("[Dry run] Would have scaled pool '{}' to {} agent(s) (currently at {})".format(
                    pool.name, new_size, pool.actual_capacity))

        if not self.dry_run and has_changes:
            self.deployments.deploy(lambda: self.deploy_pools(
                new_pool_sizes, is_scale_up), new_pool_sizes)

    def deploy_pools(self, new_pool_sizes, is_scale_up):
        from azure.mgmt.resource.resources.models import DeploymentProperties, TemplateLink

        for pool in self.agent_pools:
            if is_scale_up and pool.actual_capacity < new_pool_sizes[pool.name]:
                self.arm_parameters[pool.name +
                                    'Offset'] = {'value': pool.actual_capacity}
            self.arm_parameters[pool.name +
                                'Count'] = {'value': new_pool_sizes[pool.name]}

        if is_scale_up:
            self.prepare_template_for_scale_up(self.arm_template)

        properties = DeploymentProperties(template=self.arm_template, template_link=None,
                                          parameters=self.arm_parameters, mode='incremental')

        smc = get_mgmt_service_client(ResourceManagementClient)
        return smc.deployments.create_or_update(self.resource_group_name, "autoscaler-deployment", properties, raw=False)

    def prepare_template_for_scale_up(self, template):
        nsg_resource_index = -1
        resources = template['resources']
        for i in range(len(resources)):
            resource_type = resources[i]['type']
            if resource_type == 'Microsoft.Network/networkSecurityGroups':
                nsg_resource_index = i
            if resource_type == 'Microsoft.Network/virtualNetworks':
                dependencies = resources[i]['dependsOn']
                for j in range(len(dependencies)):
                    if dependencies[j] == "[concat('Microsoft.Network/networkSecurityGroups/', variables('nsgName'))]":
                        dependencies.pop(j)
                        break

        resources.pop(nsg_resource_index)

    def maintain(self, pods_to_schedule, running_or_pending_assigned_pods):
        """
        maintains running instances:
        - determines if idle nodes should be drained and terminated
        """

        logger.info("++++ Maintaining Nodes ++++++")

        delete_queue = []
        pods_by_node = {}
        for p in running_or_pending_assigned_pods:
            pods_by_node.setdefault(p.node_name, []).append(p)

        for pool in self.agent_pools:
            # maximum nomber of nodes we can drain without hitting our spare
            # capacity
            max_nodes_to_drain = pool.actual_capacity - self.spare_count

            for node in pool.nodes:
                state = self.get_node_state(
                    node, pods_by_node.get(node.name, []), pods_to_schedule)

                if state == ClusterNodeState.UNDER_UTILIZED_DRAINABLE:
                    if max_nodes_to_drain == 0:
                        state = ClusterNodeState.SPARE_AGENT

                logger.info("node: %-*s state: %s" % (75, node, state))

                # state machine & why doesnt python have case?
                if state in (ClusterNodeState.POD_PENDING, ClusterNodeState.BUSY,
                             ClusterNodeState.SPARE_AGENT):
                    # do nothing
                    pass
                elif state == ClusterNodeState.UNDER_UTILIZED_DRAINABLE:
                    if not self.dry_run:
                        node.cordon()
                        node.drain(pods_by_node.get(node.name, []),
                                   notifier=None)
                    else:
                        logger.info(
                            '[Dry run] Would have drained and cordoned %s', node)
                elif state == ClusterNodeState.IDLE_SCHEDULABLE:
                    if not self.dry_run:
                        node.cordon()
                    else:
                        logger.info('[Dry run] Would have cordoned %s', node)
                elif state == ClusterNodeState.BUSY_UNSCHEDULABLE:
                    # this is duplicated in original scale logic
                    if not self.dry_run:
                        node.uncordon()
                    else:
                        logger.info('[Dry run] Would have uncordoned %s', node)
                elif state == ClusterNodeState.IDLE_UNSCHEDULABLE:
                    if not self.dry_run:
                        delete_queue.append({'node': node, 'pool': pool})
                    else:
                        logger.info('[Dry run] Would have scaled in %s', node)
                elif state == ClusterNodeState.UNDER_UTILIZED_UNDRAINABLE:
                    logger.info('Undrainable pods: {}'.format(
                        pods_by_node.get(node.name, [])))
                else:
                    raise Exception("Unhandled state: {}".format(state))

        threads = []
        lock = Lock()
        for item in delete_queue:
            t = Thread(target=self.delete_node,
                        args=(item['pool'], item['node'], lock, ))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
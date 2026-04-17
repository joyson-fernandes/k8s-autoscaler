# Terraform changes for the autoscale pool

The autoscaler maintains an `autoscale.auto.tfvars` file in
`~/k8s-cluster/` that looks like:

```hcl
autoscale_workers = [
  { name = "JOY-K8S-A01", ipv4_address = "10.0.1.80", ipv4_netmask = 24 },
]
```

Terraform picks that up automatically because of the `.auto.tfvars`
suffix. To wire it into your existing cluster repo, add the following:

## 1. `variables.tf`

```hcl
variable "autoscale_workers" {
  description = "Dynamically managed A-pool — maintained by k8s-autoscaler"
  type = list(object({
    name         = string
    ipv4_address = string
    ipv4_netmask = number
  }))
  default = []
}
```

## 2. `main.tf` — add a new module block

```hcl
module "autoscale_workers" {
  source   = "./modules/vm"
  for_each = { for node in var.autoscale_workers : node.name => node }

  name             = each.value.name
  ipv4_address     = each.value.ipv4_address
  ipv4_netmask     = each.value.ipv4_netmask
  resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
  datastore_id     = data.vsphere_datastore.datastore.id
  folder           = var.folder
  num_cpus         = var.num_cpus
  memory           = var.memory
  guest_id         = data.vsphere_virtual_machine.template.guest_id
  scsi_type        = data.vsphere_virtual_machine.template.scsi_type
  network_id       = data.vsphere_network.network.id
  adapter_type     = data.vsphere_virtual_machine.template.network_interface_types[0]
  template_uuid    = data.vsphere_virtual_machine.template.id
  disk_size        = var.disk_size
  ceph_disk_size   = var.ceph_disk_size    # if your module supports it
  domain           = var.domain
  gateway          = var.gateway
  dns_servers      = var.dns_servers
  dns_suffix       = var.dns_suffix
}
```

## 3. Commit empty `autoscale.auto.tfvars` as a placeholder

```hcl
autoscale_workers = []
```

That's it. `terraform apply` with an empty list is a no-op; the
autoscaler fills it in as nodes are added.

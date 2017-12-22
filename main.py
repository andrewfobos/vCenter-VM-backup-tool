from pyVim.connect import SmartConnect
from pyVmomi import vim
from datetime import datetime
from pathlib import Path
import ssl
import time
import configparser
import logging
import re


def getVMsFromFolder(vmFolder, exclude_folders):
    if not (vmFolder.name in exclude_folders):
        for child in vmFolder.childEntity:
            if type(child).__name__ == "vim.VirtualMachine":
                if child.resourcePool:
                    try:
                        VMs.append(child)
                    except NameError:
                        VMs = []
                        VMs.append(child)
            elif type(child).__name__ == "vim.Folder":
                for VM in getVMsFromFolder(child, exclude_folders):
                    try:
                        VMs.append(VM)
                    except NameError:
                        VMs = []
                        VMs.append(VM)
    else:
        VMs = []
    return VMs

def getChildByName(parent_childEntity,childname):
    for child in parent_childEntity:
        if child.name == childname:
            return child

def trackTask(task):
    while task.info.state not in ("success", "error"):
            time.sleep(3)
    if task.info.state == "success":
        if not (task.info.result):
            return "Success"
        else:
            return task.info.result
    else:
        logging.warning(task.info.error.msg)
        return None


def takeSnapshot(VM):
    task = VM.CreateSnapshot_Task(name="pre_backup_snapshot", memory=0, quiesce=0)
    time.sleep(15)
    return trackTask(task)


def deleteSnapshot(snapshot):
    task = snapshot.RemoveSnapshot_Task(removeChildren=False)  # False ли? =/
    if trackTask(task):
        logging.debug("Snapshot removed")
        return 1
    else:
        return None


def destroyVM(VM):
    task = VM.Destroy_Task()
    if trackTask(task):
        return 1
    else:
        return None


def findVmBackups(VM, backupFolder):
    backups = []
    for child in backupFolder.childEntity:
        if child.name.find(VM.name) != -1:
            backups.append(child)
    return backups


def deleteOldBackups(VM, bkpCount, backupFolder):
    while len(findVmBackups(VM, backupFolder)) > bkpCount:
        vmBackups = findVmBackups(VM, backupFolder)
        dt = []
        for vmBackup in vmBackups:
            dt.append((datetime.strptime(vmBackup.name, VM.name + '_Backup_%Y-%m-%d_%H-%M'), vmBackup))
        if destroyVM(min(dt)[1]):
            logging.info("deleting an old Backup of VM: " + VM.name)
        else:
            logging.warning("Something gone wrong with deleting old backups of " + VM.name)
            return None


def cloneToTemplate(VM, dstFolder, dstDatastore):
    backup_snapshot = takeSnapshot(VM)
    if not (backup_snapshot):
        logging.warning("Error taking snapshot of virtual machine \""+VM.name+"\"")
        return None
    else:
        spec = vim.VirtualMachineCloneSpec()
        location = vim.VirtualMachineRelocateSpec()
        location.folder = dstFolder
        location.datastore = dstDatastore
        spec.location = location
        spec.snapshot = backup_snapshot
        spec.template = True
        spec.memory = False
        task = VM.CloneVM_Task(name=VM.name
                                    + "_Backup_"
                                    + (datetime.today()).strftime("%Y-%m-%d_%H-%M"),
                               spec=spec,
                               folder=dstFolder)
        if trackTask(task):
            logging.info("Successfull backup of VM "
                         + VM.name
                         + ", deleting Snapshot")
            deleteSnapshot(backup_snapshot)
            deleteOldBackups(VM, 2, dstFolder)
        else:
            logging.warning("Something gone wrong with coping VM \""
                            + VM.name
                            + "\". Deleting made snapshot.")
            deleteSnapshot(backup_snapshot)


def main():
    config = configparser.ConfigParser()
    try:
        if Path('/etc/vSphere_backup.conf').is_file():
            config.read('/etc/vSphere_backup.conf')

        elif Path('vSphere_backup.conf').is_file():
            config.read('vSphere_backup.conf')
        else:
            print('Can\'t locate config file at:\n\t* /etc/vSphere_backup.conf\n\t* ./vSphere_backup.conf')
            exit(1)
    except configparser.Error as error:
        print(error)
        exit(1)
    numeric_level = getattr(logging, config.get('logging',
                                                'log_level',
                                                fallback='DEBUG').upper(),None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % config.get('logging', 'log_level', fallback='DEBUG'))

    logging.basicConfig(filename=config.get('logging',
                                            'log_file',
                                            fallback='/var/log/vSphere_backup.log'),
                    level=numeric_level,
                    format='%(asctime)s %(levelname)s: %(message)s',
                    datefmt='%d/%m/%Y %H:%M:%S ')

    s = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
    s.verify_mode = ssl.CERT_NONE
    logging.info(" ")
    logging.info("*************NEW RUN*************")
    logging.info("VMWare vSphere VM backup script started")
    starttime = datetime.now()

    try:
        global c
        c = SmartConnect(host=config.get('vSphere', 'vshost'), user=config.get('vSphere', 'vsuser'),
                     pwd=config.get('vSphere', 'vspasswd'), sslContext=s)
    except vim.fault.InvalidLogin:
        logging.fatal("Invalid Login/Password or not enough permissions for user.")
        exit(1)
    except TimeoutError:
        logging.fatal("Host Timeout")
        exit(1)
    except ssl.SSLError:
        logging.fatal("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed.")
        exit(1)
    except configparser.NoOptionError:
        logging.fatal("Configuration inconsistent, check configuration file in block \"vSphere\".")
        exit(1)

    datacenter = getChildByName(c.content.rootFolder.childEntity, "ip-home")
    if not(datacenter):
        logging.fatal("Datacenter not found.")
        exit(1)
    logging.debug("datacenter: "+datacenter.name)
    vmFolder = datacenter.vmFolder
    backupFolder = getChildByName(vmFolder.childEntity, config.get('backup', 'backup_folder', fallback='Backups'))
    if not(backupFolder):
        logging.fatal("No such folder in vSphere datacenter. Did you created it?")
        exit(1)
    logging.debug("backupFolder: "+backupFolder.name)
    backupDatastore = getChildByName(datacenter.datastore, config.get('backup', 'backup_datastore', fallback=''))
    if not(backupDatastore):
        logging.fatal("No such datastore registred in vSphere.")
        exit(1)
    logging.debug("backupDatastore: "+backupDatastore.name)
    noBackupFolders = re.split(r'\s*,\s*',config.get('backup', 'exclude_folders', fallback=''))
    logging.debug("noBackupFolders "+str(noBackupFolders))
    noBackupVMs = re.split(r'\s*,\s*',config.get('backup', 'exclude_VMs', fallback=''))
    logging.debug("noBackupVMs "+str(noBackupVMs))
    VMs = getVMsFromFolder(vmFolder, noBackupFolders)
    logging.debug("VMs count:"+str(len(VMs)))
    for VM in VMs:
        if VM.name not in noBackupVMs:
            logging.debug("Preparing to backup VM: "+VM.name)
            cloneToTemplate(VM,backupFolder,backupDatastore)
#    VM = getChildByName(VMs,"VMware vCenter Server Appliance")
#    print(VM.name)
#    cloneToTemplate(VM,backupFolder,backupDatastore)
    endtime = datetime.now()-starttime
    logging.info("Execution time: "+str(endtime))

if __name__ == "__main__":
    main()
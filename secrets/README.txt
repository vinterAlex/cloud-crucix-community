# Put your service-account JSON key in this folder.

Any *.json file here is picked up automatically at startup; the file name does
not matter as long as it ends in .json.

  Example:
      secrets\my-key.json

Your IT / cloud team can create a read-only service-account key for you. See
PERMISSIONS.txt in the project root for the exact IAM roles to grant.

Never commit a real key to source control.

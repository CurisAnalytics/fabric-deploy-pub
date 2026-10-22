# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "00000000-0000-0000-0000-0000000000a1",
# META       "default_lakehouse_name": "fabdep_lakehouse",
# META       "default_lakehouse_workspace_id": "00000000-0000-0000-0000-0000000000de",
# META       "known_lakehouses": [
# META         {
# META           "id": "00000000-0000-0000-0000-0000000000a1"
# META         }
# META       ]
# META     }
# META   }
# META }

# CELL ********************

df = spark.read.table("test_a")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

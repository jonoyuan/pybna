###################################################################
# This is the class that manages destinations for the pyBNA object
###################################################################
import os
import yaml
import psycopg2
from psycopg2 import sql
from tqdm import tqdm
import random, string

from .dbutils import DBUtils
from .destinationcategory import DestinationCategory


class Destinations(DBUtils):
    """pyBNA Destinations class"""

    def __init__(self):
        DBUtils.__init__(self,"")
        self.config = None
        self.verbose = None
        self.debug = None
        self.srid = None
        self.db_connectivity_table = None
        self.destinations = None


    def register_destinations(self,category=None,workspace_schema=None,destinations=None):
        """
        Wrapper function that registers destinations and then assigns maxscore

        args
        category -- a destination category to register. None -> re-register all destinations
        workspace_schema -- schema to save interim working tables to
        destinations -- a list of destinations (if none, use the config file)
        """
        if category is None and destinations is None:
            if self.verbose:
                print('Adding destinations')
            self.destinations = dict()

        if destinations is None:
            destinations = [{
                "name": "overall",
                "subcats": self.config.bna.destinations
            }]

        self._register(destinations=destinations,category=category,workspace_schema=workspace_schema)

        # assign maxpoints
        for name, destination in self.destinations.items():
            if (
                (
                    category is None or
                    category == name
                ) and
                destination.has_subcats
                ):
                destination.maxpoints = self._get_maxpoints(destination)


    def _register(self,destinations,category=None,workspace_schema=None):
        """
        Retrieve the destinations identified in the config file and register them.

        args
        destinations -- a list of destinations
        category -- a destination category to register. None -> re-register all destinations
        workspace_schema -- schema to save interim working tables to
        """
        for v in destinations:
            config = self.parse_config(v)
            if category is None or config.name == category:
                self.destinations[config.name] = DestinationCategory(
                    config,
                    self.db_connection_string,
                    workspace_schema=workspace_schema
                )
            if "subcats" in config:
                self._register(
                    category=category,
                    workspace_schema=workspace_schema,
                    destinations=config.subcats
                )


    def score_destinations(self,output_table,scenario_id=None,subtract=False,with_geoms=False,overwrite=False,dry=None):
        """
        Creates a new db table of scores for each block

        args:
        output_table -- table to create (optionally schema-qualified)
        scenario_id -- the id of the scenario for which scores are calculated
            (none means the scores represent the base condition)
        subtract -- if true the calculated scores for the scenario represent
            a subtraction of that scenario from all other scenarios
        overwrite -- overwrite a pre-existing table
        dry -- a path to save SQL statements to instead of executing in DB
        """
        # make a copy of sql substitutes
        subs = dict(self.sql_subs)

        # check if a scenarios column exists
        if scenario_id is None:
            try:
                self.get_column_type(self.db_connectivity_table,"scenario")
                subs["scenario_where"] = sql.SQL("WHERE scenario IS NULL")
            except:
                subs["scenario_where"] = sql.SQL("")
        else:
            subs["scenario_id"] = sql.Literal(scenario_id)

        schema, output_table = self.parse_table_name(output_table)

        subs["scores_table"] = sql.Identifier(output_table)
        if schema is None:
            schema = self.get_default_schema()
        subs["scores_schema"] = sql.Identifier(schema)

        conn = self.get_db_connection()
        cur = conn.cursor()

        if dry is None:
            if overwrite:
                self.drop_table(
                    table=output_table,
                    schema=schema,
                    conn=conn
                )
            elif self.table_exists(output_table,subs["scores_schema"].as_string(conn)):
                raise psycopg2.ProgrammingError("Table {}.{} already exists".format(subs["scores_schema"].as_string(conn),output_table))

        # create temporary filtered connectivity table
        if scenario_id is None:
            self._run_sql_script("01_connectivity_table.sql",subs,["sql","destinations"],conn=conn)
        elif subtract:
            self._run_sql_script("01_connectivity_table_scenario_subtract.sql",subs,["sql","destinations"],conn=conn)
        else:
            self._run_sql_script("01_connectivity_table_scenario.sql",subs,["sql","destinations"],conn=conn)

        # generate high and low stress counts for all categories
        print("Counting destinations for each block")
        columns = sql.SQL("")
        tables = sql.SQL("")
        for name, destination in self.destinations.items():
            if destination.has_count:
                print(("   ...{}".format(name)))
                destination.count_connections(subs,conn=conn)
                destination.calculate_score(subs,conn=conn)
                columns += sql.SQL("""
                    ,{table}.hs AS {hs}
                    ,{table}.ls AS {ls}
                    ,{table}.score AS {score}
                """).format(**{
                    "table": sql.Identifier(destination.workspace_table),
                    "hs": sql.Identifier(name + "_hs"),
                    "ls": sql.Identifier(name + "_ls"),
                    "score": sql.Identifier(name + "_score")
                })
                tables += sql.SQL("""
                    LEFT JOIN {schema}.{table} ON blocks.{blocks_id_col} = {table}.block_id
                """).format(**{
                    "schema": sql.Identifier(destination.workspace_schema),
                    "table": sql.Identifier(destination.workspace_table),
                    "blocks_id_col": self.sql_subs["blocks_id_col"]
                })

        for name, destination in self.destinations.items():
            if destination.has_subcats:
                columns += sql.SQL("""
                    ,NULL::FLOAT AS {score}
                """).format(**{
                    "score": sql.Identifier(name + "_score")
                })

        print("Compiling destination data for all sources into output table")
        subs["columns"] = columns
        subs["tables"] = tables
        self._run_sql_script("04_all_combined.sql",subs,["sql","destinations"],dry=dry,conn=conn)

        # finally set any category scores
        print("Calculating category scores")
        self.aggregate_subcategories(self.destinations["overall"],subs,conn=conn)

        if with_geoms:
            self._copy_block_geoms(conn,subs,dry)

        conn.commit()
        conn.close()


    def aggregate_subcategories(self,destination,subs,conn):
        """
        Iteratively calculates category scores from all component subcategories
        using the weights defined in the config file.
        Will first calculate any subcategories which themselves have subcategories.

        args
        destination -- the destination to calculate subcategory scores for
        subs -- list of SQL substitutions from the parent method
        conn -- psycopg2 connection object from the parent method
        """
        if "subcats" in destination.config:
            for subcat in destination.config.subcats:
                self.aggregate_subcategories(self.destinations[subcat["name"]],subs,conn)

            if self.verbose:
                print("   ... {}".format(destination.config.name))

            num = []
            den = []
            check_zero = []
            check_null = []
            for subcat in destination.config.subcats:
                d = self.destinations[subcat["name"]]
                num.append(sql.SQL("{}*coalesce({},0)::float/{}").format(
                    sql.Literal(d.config.weight),
                    sql.Identifier(d.config.name + "_score"),
                    sql.Literal(d.maxpoints)
                ))
                den.append(sql.SQL("case when {} is null then 0 else {} end").format(
                    sql.Identifier(d.config.name + "_score"),
                    sql.Literal(d.config.weight)
                ))
                check_zero.append(sql.SQL("coalesce({},0) = 0").format(
                    sql.Identifier(d.config.name + "_score")
                ))
                check_null.append(sql.SQL("{} is null").format(
                    sql.Identifier(d.config.name + "_score")
                ))

            subs["this_column"] = sql.Identifier(destination.config.name + "_score")
            subs["check_null"] = sql.SQL(" and ").join(check_null)
            subs["check_zero"] = sql.SQL(" and ").join(check_zero)
            subs["numerator"] = sql.SQL(" + ").join(num)
            subs["denominator"] = sql.SQL(" + ").join(den)
            subs["maxpoints"] = sql.Literal(destination.maxpoints)
            q = sql.SQL("""
                update {scores_schema}.{scores_table}
                set
                    {this_column} =
                        case
                            when {check_null} then null
                            when {check_zero} then 0
                            else {maxpoints} * ({numerator})::FLOAT/({denominator})
                            end
            """).format(**subs)

            self._run_sql(q.as_string(conn),conn=conn)


    def _get_maxpoints(self,destination,subcategory=False):
        """
        calculates a maximum score for main categories composed of subcategories
        using the weights assigned to the subcategories.
        """
        if subcategory:
            if "weight" in destination.config:
                return destination.config.weight
            elif destination.maxpoints is not None:
                return destination.maxpoints
            elif destination.has_subcats:
                maxpoints = 0
                for subcat in destination.config.subcats:
                    maxpoints += self._get_maxpoints(self.destinations[subcat["name"]],subcategory=True)
                return maxpoints
            else:
                raise ValueError("Unable to get maxpoints for category {}".format(destination.config.name))
        else:
            if destination.maxpoints is not None:
                return destination.maxpoints
            elif destination.has_subcats:
                maxpoints = 0
                for subcat in destination.config.subcats:
                    maxpoints += self._get_maxpoints(self.destinations[subcat["name"]],subcategory=True)
                return maxpoints
            else:
                raise ValueError("Unable to get maxpoints for category {}".format(destination.config.name))


    def _copy_block_geoms(self,conn,subs,dry=None):
        """
        Copies the geometries from the block table to the output table of destination
        scores.

        args
        conn -- psycopg2 connection object from the parent method
        subs -- list of SQL substitutions from the parent method
        """
        # get geometry type from block table
        subs["type"] = sql.SQL(
            self.get_column_type(
                subs["blocks_table"].as_string(conn),
                subs["blocks_geom_col"].string,
                subs["blocks_schema"].as_string(conn)
            )
        )
        subs["sidx_name"] = sql.Identifier("sidx_")+subs["scores_table"]

        self._run_sql_script("05_add_geoms.sql",subs,["sql","destinations"],dry=dry,conn=conn)

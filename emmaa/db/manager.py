from collections import defaultdict

from fnvhash import fnv1a_32
from sqlalchemy.exc import IntegrityError

__all__ = ['EmmaaDatabaseManager', 'EmmaaDatabaseError']

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .schema import EmmaaTable, User, Query, Base, Result, UserQuery
from emmaa.queries import Query as QueryObject

logger = logging.getLogger(__name__)


class EmmaaDatabaseError(Exception):
    pass


class EmmaaDatabaseSessionManager(object):
    """A Database session context manager that is used by EmmaaDatabaseManager.
    """
    def __init__(self, host, engine):
        logger.debug(f"Grabbing a session to {host}...")
        DBSession = sessionmaker(bind=engine)
        logger.debug("Session grabbed.")
        self.session = DBSession()
        if self.session is None:
            raise EmmaaDatabaseError("Could not acquire session.")
        return

    def __enter__(self):
        return self.session

    def __exit__(self, exception_type, exception_value, traceback):
        if exception_type:
            logger.exception(exception_value)
            logger.info("Got exception: rolling back.")
            self.session.rollback()
        else:
            logger.debug("Committing changes...")
            self.session.commit()

        # Close the session.
        self.session.close()


class EmmaaDatabaseManager(object):
    """A class used to manage sessions with EMMAA's database."""
    table_order = ['user', 'query', 'user_query', 'result']

    def __init__(self, host, label=None):
        self.host = host
        self.label = label
        self.engine = create_engine(host)
        self.tables = {tbl.__tablename__: tbl
                       for tbl in EmmaaTable.__subclasses__()}
        self.session = None
        return

    def get_session(self):
        return EmmaaDatabaseSessionManager(self.host, self.engine)

    def create_tables(self, tables=None):
        """Create the tables from the EMMAA database

        Optionally specify `tables` to be created. List may contain either
        table objects or the string names of the tables.
        """
        # Regularize the type of input to name strings.
        if tables is not None:
            tables = [tbl.__tablename__ if isinstance(tbl, EmmaaTable) else tbl
                      for tbl in tables]

        if tables is None:
            tables = set(self.tables.keys())
        else:
            tables = set(tables)

        for tbl_name in self.table_order:
            if tbl_name in tables:
                logger.info(f"Creating {tbl_name} table")
                if not self.tables[tbl_name].__table__.exists(self.engine):
                    self.tables[tbl_name].__table__.create(bind=self.engine)
                    logger.debug("Table created!")
                else:
                    logger.warning(f"Table {tbl_name} already exists! "
                                   f"No action taken.")
        return

    def drop_tables(self, tables=None, force=False):
        """Drop the tables from the EMMAA database given in `tables`.

        If `tables` is None, all tables will be dropped. Note that if `force`
        is False, a warning prompt will be raised to asking for confirmation,
        as this action will remove all data from that table.
        """
        # Regularize the type of input to table objects.
        if tables is not None:
            tables = [tbl if isinstance(tbl, EmmaaTable) else self.tables[tbl]
                      for tbl in tables]

        if not force:
            # Build the message
            if tables is None:
                msg = ("Do you really want to clear the %s database? [y/N]: "
                       % self.label)
            else:
                msg = "You are going to clear the following tables:\n"
                msg += '\n'.join(['\t-' + tbl.__tablename__ for tbl in tables])
                msg += '\n'
                msg += ("Do you really want to clear these tables from %s? "
                        "[y/N]: " % self.label)

            # Check to make sure.
            resp = input(msg)
            if resp != 'y' and resp != 'yes':
                logger.info('Aborting drop.')
                return False

        if tables is None:
            logger.info("Removing all tables...")
            Base.metadata.drop_all(self.engine)
            logger.debug("All tables removed.")
        else:
            for tbl in tables:
                logger.info("Removing %s..." % tbl.__tablename__)
                if tbl.__table__.exists(self.engine):
                    tbl.__table__.drop(self.engine)
                    logger.debug("Table removed.")
                else:
                    logger.debug("Table doesn't exist.")
        return True

    def add_user(self, user_id, email):
        """Add a new user's email and id to Emmaa's User table."""
        try:
            new_user = User(id=user_id, email=email)
            with self.get_session() as sess:
                sess.add(new_user)
        except IntegrityError as e:
            logger.warning(f"A user with email {email} already exists.")
        return user_id

    def put_queries(self, user_email, user_id, query, model_ids,
                    subscribe=True):
        """Add queries to the database for a given user.

        Parameters
        ----------
        user_email : str
            the email of the user that entered the queries.
        user_id : int
            the user id of the user that entered the queries. Corresponds to
            the user id in the User table in indralab_auth_tools
        query : emmaa.queries.Query
            A query object containing all necessary information.
        model_ids : list[str]
            A list of the short, standard model IDs to which the user wishes
            to apply these queries.
        subscribe : bool
            True if the user wishes to subscribe to this query.
        """
        logger.info(f"Got request to put query {query} for {user_email} "
                    f"for {model_ids} with subscribe={subscribe}")

        # Make sure model_ids is a list.
        if not isinstance(model_ids, list) and not isinstance(model_ids, set):
            raise TypeError("Invalid type: %s. Must be list or set."
                            % type(model_ids))

        if not subscribe:
            logger.info("Not subscribing...")

        # Check if anonymous user
        if not user_email and not user_id:
            logger.info(f'User {user_email} is not registered in the user '
                        'database. Query will be stored as anonymous query.')
            # Make sure user_id is a None and not any other object
            # evaluating to False (only None register as NULL in table)
            user_email = 'anonymous@emmaa.bio'
            user_id = None

        # Open database session
        with self.get_session() as sess:
            # Get existing hashes, user's id and user's subscriptions
            existing_hashes = {h for h, in sess.query(Query.hash).all()}
            existing_user_queries = {h for h, in sess.query(
                UserQuery.query_hash).filter(UserQuery.user_id == user_id)}

            # Check if logged in user is in the emmaa user table
            if user_email and user_id:
                res = \
                    sess.query(User.id).filter(User.id == user_id).first()
                if res:
                    logger.info(f'User {user_email} is registered in the '
                                f'user table.')
                else:
                    logger.info(f'{user_email} not in user table. Adding...')
                    self.add_user(user_id=user_id, email=user_email)

            new_queries = []
            new_user_queries = []
            for model_id in model_ids:
                qh = query.get_hash_with_model(model_id)

                # Add to queries if not present
                if qh not in existing_hashes:
                    logger.info(f"Adding query on {model_id} to the db.")
                    new_queries.append(Query(model_id=model_id,
                                             json=query.to_json(),
                                             hash=qh))
                else:
                    logger.info(f"Query for {model_id} already in db.")

                # Add query to UserQuery table or update existing one
                if qh not in existing_user_queries:
                    new_user_queries.append(UserQuery(user_id=user_id,
                                                      query_hash=qh,
                                                      subscription=subscribe,
                                                      count=1))
                    logger.info(f'Registering query on {model_id} for user '
                                f'{user_email}')
                # Update existing query
                else:
                    user_query = sess.query(UserQuery).filter(
                        UserQuery.user_id == user_id,
                        UserQuery.query_hash == qh
                    ).first()
                    logger.info(f'Updating existing query for {user_email} '
                                f'on {model_id} ({qh})')
                    # Update subscription
                    # Set subscribe to True, handle un-subscribe elsewhere
                    if subscribe:
                        user_query = update_subscription(user_query, subscribe)

                    # Update query count
                    user_query.count += 1

            # Add new queries and register them for the user
            sess.add_all(new_queries)
            sess.add_all(new_user_queries)
        return

    def get_queries(self, model_id):
        """Get queries that refer to the given model_id.

        Parameters
        ----------
        model_id : str
            The short, standard model ID.

        Returns
        -------
        queries : list[emmaa.queries.Query]
            A list of queries retrieved from the database.
        """
        # TODO: check whether a query is registered or not.
        with self.get_session() as sess:
            q = sess.query(Query.json).filter(Query.model_id == model_id)
            queries = [QueryObject._from_json(q) for q, in q.all()]
        return queries

    def put_results(self, model_id, query_results):
        """Add new results for a set of queries tested on a model_id.

        Parameters
        ----------
        model_id : str
            The short, standard model ID.
        query_results : list of tuples
            A list of tuples of the form (query, result_json), where
            the query is the query object run against the model,
            and the result_json is the json containing corresponding result.
        """
        results = []
        for query, mc_type, result_json in query_results:
            query_hash = query.get_hash_with_model(model_id)
            results.append(Result(query_hash=query_hash,
                                  mc_type=mc_type,
                                  result_json=result_json))

        with self.get_session() as sess:
            sess.add_all(results)
        return

    def get_results_from_query(self, query, model_ids, latest_order=1):
        logger.info(f"Got request for results of {query} on {model_ids}.")
        hashes = {query.get_hash_with_model(model_id) for model_id in model_ids}
        with self.get_session() as sess:
            q = (sess.query(Query.model_id, Query.json, Result.mc_type,
                            Result.result_json, Result.date)
                 .filter(Result.query_hash.in_(hashes),
                         Query.hash == Result.query_hash))
            results = _make_queries_in_results(q.all())
            results = _weed_results(results, latest_order=latest_order)
        logger.info(f"Found {len(results)} results.")
        return results

    def get_results(self, user_email, latest_order=1):
        """Get the results for which the user has registered.

        Parameters
        ----------
        user_email : str
            The email of a user.

        Returns
        -------
        results : list[tuple]
            A list of tuples, each of the form: (model_id, query, mc_type,
            result_json, date) representing the result of a query run on a
            model on a given date.
        """
        logger.info(f"Got request for results for {user_email}")
        with self.get_session() as sess:
            q = (sess.query(Query.model_id, Query.json, Result.mc_type,
                            Result.result_json, Result.date)
                 .filter(Query.hash == Result.query_hash,
                         Query.hash == UserQuery.query_hash,
                         UserQuery.user_id == User.id,
                         UserQuery.subscription,
                         User.email == user_email))
            results = _make_queries_in_results(q.all())
            results = _weed_results(results, latest_order=latest_order)
        logger.info(f"Found {len(results)} results.")
        return results

    def get_users(self, query, model_id):
        logger.info(f"Got request for users for {query} in {model_id}.")
        with self.get_session() as sess:
            q = (sess.query(User.email).filter(
                    User.id == UserQuery.user_id,
                    Query.hash == query.get_hash_with_model(model_id)))
            users = [q for q, in q.all()]
        return users


def _weed_results(result_iter, latest_order=1):
    # Each element of result_iter: (model_id, query(object), result_json, date)
    result_dict = defaultdict(list)
    for res in result_iter:
        result_dict[(res[1].get_hash_with_model(res[0]), res[2])].append(
            tuple(res))
    sorted_results = [sorted(res_list, key=lambda r: r[-1])
                      for res_list in result_dict.values()]
    results = [result[-latest_order] for result in sorted_results]
    return results


def _make_queries_in_results(result_iter):
    # Each element of result_iter: (model_id, query_json, result_json, date)
    # Replace query_json with Query object
    results = []
    for res in result_iter:
        query = QueryObject._from_json(res[1])
        results.append((res[0], query, res[2], res[3], res[4]))
    return results


def sorted_json_string(json_thing):
    """Produce a string that is unique to a json's contents."""
    if isinstance(json_thing, str):
        return json_thing
    elif isinstance(json_thing, list):
        return '[%s]' % (','.join(sorted(sorted_json_string(s)
                                         for s in json_thing)))
    elif isinstance(json_thing, dict):
        return '{%s}' % (','.join(sorted(k + sorted_json_string(v)
                                         for k, v in json_thing.items())))
    elif isinstance(json_thing, float):
        return str(json_thing)
    else:
        raise TypeError(f"Invalid type: {type(json_thing)}")


def hash_query(query_json, model_id):
    """Create an FNV-1a 32-bit hash from the query json and model_id."""
    unique_string = model_id + ':' + sorted_json_string(query_json)
    return fnv1a_32(unique_string.encode('utf-8'))


def update_subscription(user_query, new_sub_status):
    """Update a UserQuery object's subscription status

    user_query : `emmaa.db.schema.UserQuery`
        The UserQuery object to be updated
    new_sub_status : Bool
        The subscription status to change to

    Returns
    -------
    user_query : UserQuery(object)
        The updated UserQuery object
    """
    if new_sub_status is not user_query.subscription:
        user_query.subscription = new_sub_status
        logger.info(f'Updated subscription status to '
                    f'{new_sub_status} for query {user_query.query_hash}')
    return user_query

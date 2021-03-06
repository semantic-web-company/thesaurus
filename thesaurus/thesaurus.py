import numpy as np
import os, sys
import time
from collections import defaultdict
from time import time
import logging
from functools import lru_cache
import rdflib
from rdflib.namespace import SKOS
import requests
import pickle
import scipy, scipy.sparse
import pp_api
from datetime import datetime


logging.basicConfig(format='%(name)s at %(asctime)s: %(message)s')
log_level_str = os.environ.get('LOG_LEVEL', logging.WARNING)
log_level = getattr(logging, str(log_level_str), logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(log_level)


class Thesaurus(rdflib.graph.Graph):
    """
    A class for thesauri. Especially useful if you have a corpus for tagging
    with this thesaurus.
    
    In order to get the cumulative concept frequencies you may want to call
     `query_and_add_cpt_frequencies` method.
    """

    own_freq_predicate = rdflib.URIRef(':own_frequency')
    cum_freq_predicate = rdflib.URIRef(':cum_frequency')

    def __init__(self, lang='en', *args, **kwargs):
        super().__init__(*args, **kwargs)
        top_uri = rdflib.URIRef(':T')
        self.add([top_uri,
                  rdflib.namespace.SKOS.prefLabel,
                  rdflib.Literal(':T', lang=lang)])
        self.add([top_uri,
                  rdflib.namespace.RDF.type,
                  rdflib.namespace.SKOS.Concept])
        scheme_uri = rdflib.URIRef(':scheme')
        self.add((scheme_uri,
                  rdflib.namespace.RDF.type,
                  rdflib.namespace.SKOS.ConceptScheme))
        self.add([scheme_uri,
                  rdflib.namespace.DCTERMS.title,
                  rdflib.Literal(':scheme', lang=lang)])
        self.add((top_uri,
                  rdflib.namespace.SKOS.topConceptOf,
                  scheme_uri))
        self.add((scheme_uri,
                  rdflib.namespace.SKOS.hasTopConcept,
                  top_uri))
        self.top_uri = top_uri
        # self.no_lcs_pairs = defaultdict(set)

    def get_all_concepts(self):
        s_uri = self.triples((None,
                              rdflib.namespace.RDF.type,
                              rdflib.namespace.SKOS.Concept))
        return {x[0] for x in s_uri}

    def get_labels(self, cpt_uri, lang='en'):
        filter_label = lambda l: ((l.language is None or l.language == lang) and
                                  not str(l).startswith(':'))
        pref_labels = filter(filter_label,
                             self[rdflib.URIRef(cpt_uri):SKOS.prefLabel:])
        alt_labels = filter(filter_label,
                            self[rdflib.URIRef(cpt_uri):SKOS.altLabel:])
        hidden_labels = filter(filter_label,
                               self[rdflib.URIRef(cpt_uri):SKOS.hiddenLabel:])
        labels = {
            SKOS.prefLabel: list(pref_labels),
            SKOS.altLabel: list(alt_labels),
            SKOS.hiddenLabel: list(hidden_labels)
        }
        return labels

    def get_all_concepts_and_labels(self, lang="en"):
        s_pl = self.triples((None,
                             rdflib.namespace.SKOS.prefLabel|rdflib.namespace.SKOS.altLabel|rdflib.namespace.SKOS.hiddenLabel,
                             None))
        uri_labels = [(str(x[0]), str(x[2])) for x in s_pl
                      if x[2].language == lang
                      if not str(x[2]).startswith(':')]
        uri2labels = dict()
        for uri, label in uri_labels:
            try:
                uri2labels[uri].append(label)
            except KeyError:
                uri2labels[uri] = [label]
        return uri2labels

    def get_all_concepts_and_pref_labels(self, lang="en"):
        s_pl = self.triples((None,
                             rdflib.namespace.SKOS.prefLabel,
                             None))
        return {str(x[0]): str(x[2]) for x in s_pl
                if x[2].language == lang
                if not str(x[2]).startswith(':')}

    def get_leaves(self):
        brs = {x[2] for x in self.triples((
            None,
            rdflib.namespace.SKOS.broader,
            None
        ))}
        leaves = set(self.get_all_concepts()) - brs
        return leaves

    # def get_pref_label(self, uri):
    #     pl = self.value(rdflib.URIRef(uri), rdflib.namespace.SKOS.prefLabel)
    #     return pl

    def add_path(self, path):
        prev_uriref = None
        for uri, pref_label in path:
            uriref = rdflib.URIRef(uri)
            self.add([uriref,
                      rdflib.namespace.SKOS.prefLabel,
                      rdflib.Literal(pref_label)])
            self.add([uriref,
                      rdflib.namespace.RDF.type,
                      rdflib.namespace.SKOS.Concept])
            if prev_uriref is not None:
                self.add([
                    uriref,
                    rdflib.namespace.SKOS.broader,
                    prev_uriref
                ])
                self.add([
                    prev_uriref,
                    rdflib.namespace.SKOS.narrower,
                    uriref
                ])
            else:
                self.add([
                    self.top_uri,
                    rdflib.namespace.SKOS.narrower,
                    uriref
                ])
                self.add([
                    uriref,
                    rdflib.namespace.SKOS.broader,
                    self.top_uri
                ])
            prev_uriref = uriref

    def add_frequencies(self, cpt_uri, cpt_freq, path=None, def_value=0):
        if path is None:
            uriref = rdflib.URIRef(cpt_uri)
            brs = {x[2] for x in self.triples((
                uriref,
                rdflib.namespace.SKOS.broader * '*',
                None
            ))}
            path = brs
        else:
            path = {x[0] for x in path}
        old_freq = self.get_own_freq(cpt_uri, def_value)
        self.set(
            (cpt_uri,
             self.own_freq_predicate,
             rdflib.Literal(old_freq+cpt_freq))
        )
        for uri in path:
            uriref = rdflib.URIRef(uri)
            old_freq = self.get_cumulative_freq(uriref, def_value)
            self.set(
                (uriref,
                 self.cum_freq_predicate,
                 rdflib.Literal(old_freq+cpt_freq))
            )

    def query_and_add_cpt_frequencies(self, sparql_endpoint, cpt_freq_graph,
                                      server=None, pid=None, auth_data=None):
        self.query_thesaurus(pid=pid, server=server, auth_data=auth_data)
        cpt_freqs = query_cpt_freqs(sparql_endpoint, cpt_freq_graph)
        for cpt_uri in cpt_freqs:
            cpt_atts = cpt_freqs[cpt_uri]
            cpt_freq = cpt_atts['frequency']
            if cpt_freq > 0:
                self.add_frequencies(cpt_uri, cpt_freq)

    def query_thesaurus(self, pid, server=None, auth_data=None, pp=None):
        assert pp or (server and auth_data)
        if pp is None:
            pp = pp_api.PoolParty(
                server=server,
                auth_data=auth_data
            )
        r = pp.export_project(pid=pid)
        self.parse(data=r, format='n3')
        top_cpts = {x[0] for x in self.triples((
            None,
            rdflib.namespace.SKOS.topConceptOf,
            None
        )) if x[0] != self.top_uri}
        for top_cpt in top_cpts:
            self.add(
                (top_cpt, rdflib.namespace.SKOS.broader, self.top_uri)
            )
            self.add(
                (self.top_uri, rdflib.namespace.SKOS.narrower, top_cpt)
            )

    @lru_cache(maxsize=None)
    def broaders(self, cpt_uri):
        path = self.triples((
            cpt_uri,
            rdflib.namespace.SKOS.broader * '+',
            None
        ))
        cpt_path = {x[2] for x in path}
        return cpt_path

    def get_lcs(self, c1_uri, c2_uri):
        c1_uri = rdflib.URIRef(c1_uri)
        c2_uri = rdflib.URIRef(c2_uri)
        if c1_uri == c2_uri:
            lcs = c1_uri
            freq = self.get_cumulative_freq(c1_uri)
        else:
            cpt_path1 = self.broaders(c1_uri)
            cpt_path2 = self.broaders(c2_uri)
            if c1_uri in cpt_path2:
                lcs = c1_uri
                freq = self.get_cumulative_freq(c1_uri)
            elif c2_uri in cpt_path1:
                lcs = c2_uri
                freq = self.get_cumulative_freq(c2_uri)
            else:
                cs = {
                    x: self.get_cumulative_freq(x)
                    for x in cpt_path1 & cpt_path2
                }
                lcs, freq = min(cs.items(),
                                key=lambda x: x[1],
                                default=[self.top_uri, float('inf')])
        return lcs, freq

    @lru_cache(maxsize=None)
    def get_cumulative_freq(self, c_uri, def_value=1):
        c_uri = rdflib.URIRef(c_uri)
        rdf_value = self.value(
            subject=c_uri,
            predicate=self.cum_freq_predicate
        )
        return rdf_value.value if rdf_value is not None else def_value

    def precompute_number_children(self):
        all_cpts = self.get_all_concepts()
        for cpt in all_cpts:
            c_uri = rdflib.URIRef(cpt)
            n_children = rdflib.Literal(len(set(self.triples(
                (c_uri, rdflib.namespace.SKOS.narrower * '*', None)
            ))))
            self.set(
                (rdflib.URIRef(cpt), self.cum_freq_predicate, n_children)
            )

    def get_own_freq(self, c_uri, def_value=1):
        c_uri = rdflib.URIRef(c_uri)
        rdf_value = self.value(
            subject=c_uri,
            predicate=self.own_freq_predicate
        )
        return rdf_value.value if rdf_value is not None else def_value

    def get_lin_similarity(self, c1_uri, c2_uri):
        if c1_uri == c2_uri:
            return 1
        c1_uri = rdflib.URIRef(c1_uri)
        c2_uri = rdflib.URIRef(c2_uri)
        lcs, lcs_freq = self.get_lcs(c1_uri, c2_uri)
        if lcs == self.top_uri:
            return 0
        else:
            c1_freq = self.get_cumulative_freq(c1_uri)
            c2_freq = self.get_cumulative_freq(c2_uri)
            top_freq = self.get_cumulative_freq(self.top_uri)
            p1 = np.log(c1_freq/top_freq)
            p2 = np.log(c2_freq/top_freq)
            p_lcs = np.log(lcs_freq/top_freq)
            if p1 + p2 == 0:
                return 1
            score = (2 * p_lcs / (p1 + p2))
            assert 0. <= score <= 1, print(lcs, score)
            return score

    def get_nx_graph(self, use_related=False):
        """
        The returned graph contains an edge (u,v)  if u is narrower than v
        :param use_related: 
        :return: 
        """
        import networkx as nx
        G = nx.DiGraph()

        nodes = [str(x) for x in self.get_all_concepts()]
        G.add_nodes_from(nodes)
        for edge in self.triples((None, rdflib.namespace.SKOS.broader, None)):
            broader = str(edge[2])
            narrower = str(edge[0])
            G.add_edge(narrower, broader)
        if use_related:
            for edge in self.triples((None, rdflib.namespace.SKOS.related, None)):
                left = str(edge[2])
                right = str(edge[0])
                G.add_edge(left, right, relation='related')
                G.add_edge(right, left, relation='related')
        return G

    def plot_layout(self):
        """
        Calculate and return positions of nodes in the taxonomy tree for 
        drawing. The labels are the prefLabels of the concepts.
        
        :return: graph, positions of nodes 
        """
        G = self.get_nx_graph()
        pos = nx.drawing.nx_agraph.graphviz_layout(
            G, prog='twopi', args='-Goverlap=scalexy -Nroot=true -Groot=:T'
        )
        return G, pos

    def get_importance_ranking(self, method='pr'):
        """
        :param method: one of: 'pr' - PageRank, 'betweenness'. More to follow. 
        :return: dict {node: score}
        """
        import networkx as nx
        G = nx.DiGraph()

        nodes = [self.get_pref_label(x).toPython() for x in
                 self.get_all_concepts()]
        G.add_nodes_from(nodes)
        for edge in self.triples((None, rdflib.namespace.SKOS.broader, None)):
            broader = self.get_pref_label(edge[0]).toPython()
            narrower = self.get_pref_label(edge[2]).toPython()
            G.add_edge(narrower, broader)

        if method == 'betweenness':
            result = nx.betweenness_centrality(G)
        elif method == 'pr':
            result = nx.pagerank_scipy(G)
        else:
            raise Exception('Method {} not implemented yet'.format(method))
        return result

    def __iter__(self):
        all_cpts = set(self.get_all_concepts())
        for x in all_cpts:
            yield x

    def __str__(self):
        out = 'Thesaurus'
        return out

    def parse_file_and_add_frequencies(self,
                                       sparql_endpoint,
                                       cpt_freq_graph,
                                       file_name,
                                       format='n3',
                                       **kwargs):
        cpt_freqs = query_cpt_freqs(sparql_endpoint, cpt_freq_graph)
        self.parse(file_name, format=format)
        for cpt_uri in cpt_freqs:
            cpt_atts = cpt_freqs[cpt_uri]
            cpt_freq = cpt_atts['frequency']
            if cpt_freq > 0:
                self.add_frequencies(cpt_uri, cpt_freq)

    def get_sim_dict(self, sim_dict_path, refresh=False):
        return get_sim_dict(sim_dict_path, self, refresh=refresh)

    @classmethod
    def get_the(cls, the_path, auth_data, server, pid,
                sparql_endpoint=None, cpt_freq_graph=None, with_freqs=True,
                refresh=False, **kwargs):
        the = cls()
        if the_path is not None and os.path.exists(the_path) and not refresh:
            logger.info('Thesaurus at {} exists, loading'.format(the_path))
            with open(the_path, 'rb') as f:
                the.parse(the_path, format='n3')
        elif with_freqs:
            logger.info('Querying thesaurus and frequencies')
            the.query_and_add_cpt_frequencies(
                auth_data=auth_data, server=server, pid=pid,
                sparql_endpoint=sparql_endpoint, cpt_freq_graph=cpt_freq_graph
            )
            the.serialize(the_path, format='n3')
        else:
            logger.info('Querying thesaurus')
            the.query_thesaurus(pid=pid, server=server, auth_data=auth_data)
            logger.info('Precomputing children')
            the.precompute_number_children()
            the.serialize(the_path, format='n3')
        return the

    @classmethod
    def get_the_pp(cls, the_path, pp, pid, refresh=False, **kwargs):
        the = cls()
        if the_path is not None and os.path.exists(the_path) and not refresh:
            logger.info('Thesaurus at {} exists, loading'.format(the_path))
            with open(the_path, 'rb') as f:
                the.parse(the_path, format='n3')
        else:
            logger.info('Querying thesaurus')
            the.query_thesaurus(pp=pp, pid=pid)
            logger.info('Precomputing children')
            the.precompute_number_children()
            if the_path is not None:
                the.serialize(the_path, format='n3')
        return the

    @classmethod
    def check_outdated(cls, the_path, pp, pid):
        """
        Check if a newer version is available at the server.

        :param the_path: path to the thesaurus file
        :param pp: PoolParty instance from github.com/artreven/pp_api
        :param pid: project id
        :return: Boolean
        """
        modified_unix = os.path.getmtime(the_path)
        modified_datetime = datetime.utcfromtimestamp(modified_unix)
        history = pp.get_history(pid=pid, from_=modified_datetime)
        return history if history else False


def create_matrix_from_dict(sim_dict, the):
    """
    Compatilibity between the two versions of the similarity dictionary pickle
    :param sim_dict: 
    :param the: 
    :return: 
    """
    all_cpts = the.get_all_concepts()
    leaves = the.get_leaves()
    all_cpts = list(leaves) + list(all_cpts - leaves)
    sim_matrix = np.eye(len(all_cpts))
    for i, cpt1 in enumerate(all_cpts):
        if i not in sim_dict.keys():
            continue
        for j, cpt2 in enumerate(all_cpts):
            if j not in sim_dict[i].keys():
                continue
            sim_matrix[i, j] = sim_dict[i][j]

    return sim_matrix, all_cpts


def get_sim_dict(sim_dict_path, the, refresh=False):
    """
    Returns a dictionary whose keys are pairs of concepts, and whose values are
    the Lin-similarity of said concepts. This is done using the thesaurus object
    passed as parameter "the"
    :param sim_dict_path:
    :param the:
    :return: sim_dict a sparse matrix whose i,j entry stores the similarity 
                      between concepts i and j
             all_cpts a list of URIs, that specifies the order in which the 
                      concepts are considered for sim_dict matrix.
              
    """
    if sim_dict_path is not None and os.path.exists(sim_dict_path) and \
            not refresh:
        logger.info('Cpt sims at {} exists, loading'.format(sim_dict_path))
        with open(sim_dict_path, 'rb') as f:
            unpickled = pickle.load(f)
            if len(unpickled) == 2:
                sim_dict, all_cpts = unpickled
            else:
                sim_dict, all_cpts = create_matrix_from_dict(unpickled, the)
    else:
        all_cpts = the.get_all_concepts()
        leaves = the.get_leaves()
        all_cpts = list(leaves) + list(all_cpts - leaves)
        sim_dict = np.eye(len(all_cpts))
        lin0_score = defaultdict(set)
        logger.info('Leaves: {}'.format(len(leaves)))
        top_cpts = [x[2] for x in the.triples(
            (the.top_uri, SKOS.narrower, None)
        )]
        cpt_clusters = [
            {
                x[2] for x in the.triples((top_cpt, SKOS.narrower * '*', None))
            }
            for top_cpt in top_cpts
        ]
        logger.info('Total clusters: {}'.format(len(cpt_clusters)))
        for i, cpt1 in enumerate(all_cpts):
            cpt1_clusters = [cluster
                             for cluster in cpt_clusters
                             if cpt1 in cluster]
            # cpt_str1 = cpt1.toPython()
            start = time()
            logger.info('Start cpt: {} {}, cpt1 clusters: {}'.format(
                i, cpt1, len(cpt1_clusters))
            )
            for j in range(i - 1 if i > 0 else 0):
                sim_dict[i, j] = sim_dict[j, i]
            c = 0
            c2 = 0
            for j, cpt2 in enumerate(all_cpts[i:]):
                # cpt_str2 = cpt2.toPython()
                if not any(cpt2 in cluster for cluster in cpt1_clusters):
                    sim_dict[i, j] = 0
                    c2 += 1
                elif cpt2 in lin0_score[cpt1] or cpt1 in lin0_score[cpt2]:
                    sim_dict[i, j] = 0
                    c += 1
                else:
                    lin_score = the.get_lin_similarity(cpt1, cpt2)
                    sim_dict[i, j] = lin_score
                    if np.isclose(lin_score, 0):
                        brs1 = the.broaders(cpt1) | {cpt1}
                        brs2 = the.broaders(cpt2) | {cpt1}
                        for ph in brs1:
                            lin0_score[ph] |= brs2
                        for ph in brs2:
                            lin0_score[ph] |= brs1
            logger.debug(
                'New done in {:0.3f}, '
                'shortcut taken {} times, '
                'shortcut2 taken {} times'.format(
                    time() - start, c, c2)
            )

        sim_dict = scipy.sparse.coo_matrix(sim_dict)
        all_cpts = [str(x) for x in all_cpts]
        if sim_dict_path is not None:
            with open(sim_dict_path, 'wb') as f:
                pickle.dump((sim_dict, all_cpts), f)
    return sim_dict, all_cpts


def query_cpt_freqs(sparql_endpoint, cpt_occur_graph):
    q_cpts = """
    select distinct ?s ?label ?freq ?cpt where {
      ?s <http://schema.semantic-web.at/ppcm/2013/5/frequencyInCorpus> ?freq .
      ?s <http://schema.semantic-web.at/ppcm/2013/5/mainLabel> ?label .
      ?s <http://schema.semantic-web.at/ppcm/2013/5/concept> ?cpt .
    }
    """
    rs = pp_api.query_sparql_endpoint(sparql_endpoint,
                                      cpt_occur_graph,
                                      q_cpts)
    results = dict()
    for r in rs:
        cpt_atts = {
            'frequency': float(r[2]),
            'mainLabel': str(r[1])
        }
        cpt_uri = r[3]
        results[cpt_uri] = cpt_atts
    return results


if __name__ == '__main__':
    import networkx as nx
    import matplotlib.pyplot as plt
    from pp_api.server_data.custom_apps import pid, server, \
        corpus_id, sparql_endpoint, pp_sparql_endpoint, p_name

    username = input('Username: ')
    pw = input('Password: ')
    auth_data = (username, pw)

    corpusgraph_id, termsgraph_id, cpt_occur_graph_id, cooc_graph = \
        pp_api.get_corpus_analysis_graphs(corpus_id)

    the = Thesaurus()
    the_path = 'misc/the.n3'
    if os.path.exists(the_path):
        the.parse(the_path, format='n3')
    else:
        the.query_thesaurus(pid=pid, server=server, auth_data=auth_data)
        the.query_and_add_cpt_frequencies(pp_sparql_endpoint + p_name,
                                          cpt_occur_graph_id,
                                          server, pid, auth_data)
        the.serialize(the_path, format='n3')

    G, pos = the.plot_layout()
    nx.draw(G, pos, with_labels=False, arrows=True, node_size=50)
    plt.title('draw_networkx')
    plt.savefig('misc/nx_test.png')

    pr = the.get_importance_ranking()
    print('PageRank: ', sorted(pr.items(),
                               key=lambda x: x[1],
                               reverse=True)[:10])

    bw = the.get_importance_ranking('betweenness')
    print('betweenness: ', sorted(bw.items(),
                                  key=lambda x: x[1],
                                  reverse=True)[:10])

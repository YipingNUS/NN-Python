Philip Bachman 23/09/2014:

"I have a reasonable implementation of the paragraph vector model up in my "nlp" repository at: "http://github.com/Philip-Bachman/NN-Python/tree/master/nlp".
There's not really a proper license in the repository, so any suggestions as to what I should do about that would be appreciated.

The PVModel class in NLModels.py implements paragraph vector, and some other stuff too. I took cues from gensim when implementing the core computations, so any significant vector/matrix ops get passed-off to multithreaded Cython code that calls BLAS functions. General corpus handling and HSM tree generation is also largely from and/or based on gensim code. If you're using the Anaconda Python distribution, you should be set in terms of dependencies.

The function test_pv_model in NLModels.py shows the basic incantations for initializing and training the paragraph vector model."
